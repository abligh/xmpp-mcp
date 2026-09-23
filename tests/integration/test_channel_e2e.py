"""Claude Code channel E2E: real stdio JSON-RPC against the ejabberd lab.

Each test spawns one or more ``python -m xmpp_mcp --channel`` subprocesses
(one per "agent", each self-registering a templated JID) and talks to them
the way Claude Code does. Inbound XMPP traffic must show up as
``notifications/claude/channel`` on stdout without any tool call.

Run with ``pytest -m agents`` (ejabberd lab) or ``pytest -m agents
--xmpp-lab prosody`` (Prosody lab: derived credentials, verified TLS).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from .conftest import LabHandle
from .helpers.stdio_mcp import ToolCallError

pytestmark = [pytest.mark.docker, pytest.mark.agents]


def _room(ej: LabHandle, prefix: str = "chan") -> str:
    # Fresh room per test: created on first join with the lab defaults
    # (non-anonymous, so occupants see real JIDs).
    return ej.room_jid(f"{prefix}-{uuid.uuid4().hex[:8]}")


# --- capability & mode -------------------------------------------------------


async def test_initialize_declares_channel_capability(spawn_agent) -> None:
    agent = await spawn_agent("cap")
    result = agent.initialize_result
    assert result["capabilities"]["experimental"] == {"claude/channel": {}}
    assert "`reply`" in result["instructions"]
    assert agent.jid in result["instructions"]


async def test_plain_mode_is_unchanged(spawn_agent, lab: LabHandle) -> None:
    """Without --channel: no capability, no pushes — the pull tools still work."""
    agent = await spawn_agent("plain", channel=False)
    assert "claude/channel" not in agent.initialize_result["capabilities"].get("experimental", {})
    async with lab.raw("alice") as alice:
        alice.send_chat(agent.jid, "poll me")
        await agent.assert_no_event(within=2.0)
        got = await agent.call("get_recent_messages")
        assert [m["body"] for m in got["messages"]] == ["poll me"]


# --- one-to-one ----------------------------------------------------------------


async def test_direct_message_is_pushed(spawn_agent, lab: LabHandle) -> None:
    agent = await spawn_agent("dm")
    async with lab.raw("alice") as alice:
        alice.send_chat(agent.jid, "please review PR 7")
        ev = await agent.next_event()
    assert ev["content"] == "please review PR 7"
    meta = ev["meta"]
    assert meta["type"] == "chat"
    assert meta["sender"].startswith("alice@xmpp.test/")
    assert meta["sender_jid"] == "alice@xmpp.test"
    assert meta["reply_to"] == meta["sender"]


async def test_buffer_still_serves_get_recent_messages(spawn_agent, lab: LabHandle) -> None:
    agent = await spawn_agent("buf")
    async with lab.raw("alice") as alice:
        alice.send_chat(agent.jid, "pushed and buffered")
        await agent.next_event()
    # search_messages is non-destructive; get_recent_messages then drains.
    assert (await agent.call("search_messages", {"query": "buffered"}))["count"] == 1
    got = await agent.call("get_recent_messages")
    assert [m["body"] for m in got["messages"]] == ["pushed and buffered"]


async def test_reply_reaches_the_sender(spawn_agent, lab: LabHandle) -> None:
    agent = await spawn_agent("rep")
    async with lab.raw("alice") as alice:
        alice.send_chat(agent.jid, "status?")
        ev = await agent.next_event()
        sent = await agent.call("reply", {"to": ev["meta"]["reply_to"], "message": "all green"})
        assert sent["type"] == "chat"
        msg = await alice.wait_for_message(timeout=5)
    assert msg.body == "all green"
    assert msg.from_jid.startswith(agent.jid + "/")


async def test_offline_messages_are_pushed_after_startup(
    spawn_agent, lab: LabHandle
) -> None:
    """Messages sent while the agent was down arrive as soon as it comes back."""
    first = await spawn_agent("off")
    name = first.agent_name
    await first.close()  # the account now exists; the agent is offline
    async with lab.raw("alice") as alice:
        alice.send_chat(first.jid, "while you were out")
        await asyncio.sleep(0.5)
    again = await spawn_agent("off", name=name)
    ev = await again.next_event(timeout=15)
    assert ev["content"] == "while you were out"


async def test_sender_gate(spawn_agent, lab: LabHandle) -> None:
    agent = await spawn_agent("gate", "--allow", "bob@xmpp.test")
    async with lab.raw("alice") as alice, lab.raw("bob") as bob:
        alice.send_chat(agent.jid, "from alice")
        await agent.assert_no_event(within=2.0)
        bob.send_chat(agent.jid, "from bob")
        ev = await agent.next_event()
    assert ev["content"] == "from bob"
    # Gated messages are still buffered for the pull tools.
    bodies = {m["body"] for m in (await agent.call("get_recent_messages"))["messages"]}
    assert bodies == {"from alice", "from bob"}


# --- MUC -----------------------------------------------------------------------


async def test_room_message_pushed_and_own_echo_suppressed(
    spawn_agent, lab: LabHandle
) -> None:
    room = _room(lab)
    agent = await spawn_agent("muc", "--join", room)
    async with lab.raw("alice") as alice:
        await alice.join_muc(room, "alice")
        # The agent's own line is reflected by the room (XEP-0045 §7.4)...
        await agent.call("send_room_message", {"room_jid": room, "body": "agent here"})
        assert (await alice.wait_for_message(timeout=5)).body == "agent here"
        # ...but never pushed back into its own session.
        await agent.assert_no_event(within=2.0)
        alice.send_groupchat(room, "hello room")
        ev = await agent.next_event()
    assert ev["content"] == "hello room"
    meta = ev["meta"]
    assert (meta["type"], meta["room"], meta["nick"]) == ("groupchat", room, "alice")
    assert meta["reply_to"] == room
    assert meta["sender_jid"] == "alice@xmpp.test"  # non-anonymous room


async def test_reply_to_room_posts_groupchat(spawn_agent, lab: LabHandle) -> None:
    room = _room(lab)
    agent = await spawn_agent("mrep", "--join", room)
    async with lab.raw("alice") as alice:
        await alice.join_muc(room, "alice")
        alice.send_groupchat(room, "who is on call?")
        ev = await agent.next_event()
        sent = await agent.call("reply", {"to": ev["meta"]["reply_to"], "message": "me"})
        assert sent["type"] == "groupchat"
        msg = await alice.wait_for_message(timeout=5)
    assert (msg.type, msg.body) == ("groupchat", "me")
    assert msg.from_jid == f"{room}/{agent.agent_name}"  # nick defaults to agent name


async def test_join_and_leave_room_tools(spawn_agent, lab: LabHandle) -> None:
    room = _room(lab)
    agent = await spawn_agent("jl")
    async with lab.raw("alice") as alice:
        await alice.join_muc(room, "alice")
        joined = await agent.call("join_room", {"room_jid": room})
        assert joined["nick"] == agent.agent_name
        alice.send_groupchat(room, "while joined")
        assert (await agent.next_event())["content"] == "while joined"
        await agent.call("leave_room", {"room_jid": room})
        await asyncio.sleep(0.5)
        alice.send_groupchat(room, "after leaving")
        await agent.assert_no_event(within=2.0)
    with pytest.raises(ToolCallError, match="Not joined"):
        await agent.call("send_room_message", {"room_jid": room, "body": "x"})


# --- agent to agent --------------------------------------------------------------


async def test_agents_converse_directly(spawn_agent) -> None:
    """The motivating use case: SendMessage between sessions, over XMPP."""
    a = await spawn_agent("alpha")
    b = await spawn_agent("beta")
    await a.call("send_message", {"to": b.jid, "body": "beta, can you run the tests?"})
    ev = await b.next_event()
    assert ev["content"] == "beta, can you run the tests?"
    assert ev["meta"]["sender_jid"] == a.jid
    await b.call("reply", {"to": ev["meta"]["reply_to"], "message": "done: 212 passed"})
    back = await a.next_event()
    assert back["content"] == "done: 212 passed"
    assert back["meta"]["sender_jid"] == b.jid


async def test_list_agents_via_directory_room(spawn_agent, lab: LabHandle) -> None:
    directory = _room(lab, "agents")
    a = await spawn_agent("dira", "--join", directory)
    b = await spawn_agent(
        "dirb", "--join", directory, env={"XMPP_DISPLAY_NAME": "Beta the Builder"}
    )
    await asyncio.sleep(1.0)  # let presence propagate

    listed = {x["address"]: x for x in (await a.call("list_agents"))["agents"]}
    assert b.jid in listed, listed
    peer = listed[b.jid]
    assert peer["jid"] == b.jid  # canonical XMPP address
    assert peer["agent_id"] == f"session-{b.agent_name}"  # internal ID
    assert peer["name"] == "Beta the Builder"  # human-facing name
    assert peer["host"] == "lab"
    assert peer["is_agent"] is True
    assert peer["presence"] == "available"
    # The room nick is the friendly name, so here the display name.
    assert peer["rooms"] == [{"room": directory, "nick": "Beta the Builder"}]
    assert a.jid not in listed  # never lists itself

    await b.call("set_presence", {"show": "dnd", "status": "deep in a refactor"})
    await asyncio.sleep(1.0)
    peer = {x["address"]: x for x in (await a.call("list_agents"))["agents"]}[b.jid]
    assert (peer["presence"], peer["status"]) == ("dnd", "deep in a refactor")

    await b.close()
    await asyncio.sleep(1.0)
    remaining = {x["address"] for x in (await a.call("list_agents"))["agents"]}
    assert b.jid not in remaining


async def test_get_identity(spawn_agent, lab: LabHandle) -> None:
    room = _room(lab)
    agent = await spawn_agent("who", "--join", room)
    me = await agent.call("get_identity")
    assert me["jid"] == agent.jid
    assert me["agent_name"] == agent.agent_name
    assert me["agent_id"] == f"session-{agent.agent_name}"
    assert me["channel"] is True
    assert me["rooms"] == [{"room": room, "nick": agent.agent_name}]
