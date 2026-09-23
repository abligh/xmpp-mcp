"""Canonical session identities and friendly names, end to end (ejabberd lab).

Agents here run as if under a Claude Code session: a session file in a
private CLAUDE_CONFIG_DIR supplies the session ID (-> the canonical JID) and
the session's name (-> the friendly name). Renaming the session means editing
that file, exactly as Claude Code does.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from xmpp_mcp.webhook_relay import RelaySettings, WebhookRelay

from .conftest import EjabberdHandle

pytestmark = [pytest.mark.docker, pytest.mark.ejabberd]


def _directory(ej: EjabberdHandle) -> str:
    return ej.room_jid(f"dir-{uuid.uuid4().hex[:8]}")


def _rename(session_file: Path, name: str, source: str = "user") -> None:
    data = json.loads(session_file.read_text())
    data.update(name=name, nameSource=source)
    session_file.write_text(json.dumps(data))


async def _until(predicate, timeout: float = 10.0, every: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        result = await predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(every)


async def test_canonical_jid_and_friendly_name(spawn_agent, ejabberd: EjabberdHandle) -> None:
    room = _directory(ejabberd)
    rev = await spawn_agent("rev", "--join", room, claude_name="Reviewer")
    me = await rev.call("get_identity")
    assert me["jid"] == rev.jid  # <session id>.lab@xmpp.test — canonical
    assert (me["name"], me["name_source"]) == ("Reviewer", "auto")
    assert me["rooms"] == [{"room": room, "nick": "Reviewer"}]  # the nick is friendly


async def test_peers_see_the_name_and_can_address_by_it(
    spawn_agent, ejabberd: EjabberdHandle
) -> None:
    room = _directory(ejabberd)
    rev = await spawn_agent("rev", "--join", room, claude_name="Reviewer")
    bld = await spawn_agent("bld", "--join", room, claude_name="Builder")

    async def listed():
        agents = {a["address"]: a for a in (await bld.call("list_agents"))["agents"]}
        return agents.get(rev.jid)

    peer = await _until(listed)
    assert (peer["name"], peer["name_source"]) == ("Reviewer", "auto")

    # Addressed by friendly name; routed (and replied to) by canonical JID.
    sent = await bld.call("send_message", {"to": "Reviewer", "body": "PR 7 is ready"})
    assert sent["to"] == rev.jid
    ev = await rev.next_event()
    assert ev["content"] == "PR 7 is ready"
    assert ev["meta"]["sender_jid"] == bld.jid
    assert ev["meta"]["sender_name"] == "Builder"
    await rev.call("reply", {"to": "builder", "message": "on it"})  # any case
    assert (await bld.next_event())["content"] == "on it"


async def test_a_rename_propagates(spawn_agent, ejabberd: EjabberdHandle) -> None:
    """derived -> auto/user rename: presence, directory and room nick follow."""
    room = _directory(ejabberd)
    rev = await spawn_agent("rev", "--join", room, claude_name="bridge-cse-0123-18")
    bld = await spawn_agent("bld", "--join", room, claude_name="Builder")
    _rename(rev.session_file, "Reviewer", "user")

    async def renamed():
        agents = {a["address"]: a for a in (await bld.call("list_agents"))["agents"]}
        peer = agents.get(rev.jid)
        return peer if peer and peer["name"] == "Reviewer" else None

    peer = await _until(renamed)
    assert peer["name_source"] == "user"
    assert peer["rooms"] == [{"room": room, "nick": "Reviewer"}]  # the nick moved too
    me = await rev.call("get_identity")
    assert me["rooms"] == [{"room": room, "nick": "Reviewer"}]
    assert me["jid"] == rev.jid  # the canonical address never changes
    # And the new name is immediately addressable.
    await bld.call("send_message", {"to": "Reviewer", "body": "hello again"})
    assert (await rev.next_event())["content"] == "hello again"


async def test_same_friendly_name_twice(spawn_agent, ejabberd: EjabberdHandle) -> None:
    """Two sessions called Reviewer: both get in, and the name becomes ambiguous."""
    room = _directory(ejabberd)
    one = await spawn_agent("one", "--join", room, claude_name="Reviewer")
    two = await spawn_agent("two", "--join", room, claude_name="Reviewer")
    nicks = {(await a.call("get_identity"))["rooms"][0]["nick"] for a in (one, two)}
    assert nicks == {"Reviewer", "Reviewer (lab)"}
    watcher = await spawn_agent("watch", "--join", room, claude_name="Watcher")

    async def both_listed():
        names = [a["name"] for a in (await watcher.call("list_agents"))["agents"]]
        return names.count("Reviewer") == 2

    await _until(both_listed)
    with pytest.raises(Exception, match="ambiguous"):
        await watcher.call("send_message", {"to": "Reviewer", "body": "which one?"})
    # The canonical JID is always unambiguous.
    await watcher.call("send_message", {"to": two.jid, "body": "you, specifically"})
    assert (await two.next_event())["content"] == "you, specifically"


# --- the relay routes to friendly names --------------------------------------


@pytest_asyncio.fixture
async def named_relay(ejabberd: EjabberdHandle, tmp_path: Path):
    directory = _directory(ejabberd)
    routes = tmp_path / "routes.toml"
    routes.write_text('''
[[route]]
name = "prs-to-reviewer"
provider = "github"
event = "pull_request"
match = { "repository.full_name" = "abligh/*", action = "opened" }
to = "Reviewer"
''')
    acct = ejabberd.accounts["webhook"]
    relay = WebhookRelay(RelaySettings(  # type: ignore[call-arg]
        _env_file=None, xmpp_jid=acct.jid, xmpp_password=acct.password,
        xmpp_host=ejabberd.host, xmpp_port=ejabberd.c2s_port, xmpp_tls_insecure=True,
        token="t0ken", routes=str(routes), directory_room=directory,
    ))
    await relay.start()
    await asyncio.wait_for(relay.online.wait(), timeout=15)
    server = TestServer(relay.make_app())
    await server.start_server()
    try:
        yield relay, server, directory
    finally:
        await server.close()
        await relay.stop()


async def test_route_table_delivers_to_a_friendly_name(named_relay, spawn_agent) -> None:
    relay, server, directory = named_relay
    rev = await spawn_agent("rev", "--join", directory, claude_name="Reviewer")
    await _until(lambda: asyncio.sleep(0, result=bool(
        relay.directory.resources(directory).get("Reviewer"))))
    payload = {"action": "opened", "repository": {"full_name": "abligh/xmpp-mcp"},
               "pull_request": {"number": 9, "title": "Friendly routing", "html_url": "u"}}
    async with aiohttp.ClientSession() as http:
        resp = await http.post(server.make_url("/"), json=payload, headers={
            "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": uuid.uuid4().hex,
            "Authorization": "Bearer t0ken"})
        got = await resp.json()
    assert got["routed_by"] == "routes" and got["targets"] == [{"to": "Reviewer", "kind": "agent"}]
    ev = await rev.next_event()
    assert '"Friendly routing"' in ev["content"]
    assert ev["meta"]["sender_jid"] == "webhook@xmpp.test"


async def test_envelope_to_a_friendly_name(named_relay, spawn_agent) -> None:
    relay, server, directory = named_relay
    bld = await spawn_agent("bld", "--join", directory, claude_name="Builder")
    await _until(lambda: asyncio.sleep(0, result=bool(
        relay.directory.resources(directory).get("Builder"))))
    async with aiohttp.ClientSession() as http:
        resp = await http.post(server.make_url("/"), headers={"X-Webhook-Token": "t0ken"},
                               json={"xmpp": {"to": "Builder"}, "deploy": "done"})
        assert (await resp.json())["routed_by"] == "envelope"
    ev = await bld.next_event()
    assert '"deploy":"done"' in ev["content"] and '"xmpp"' not in ev["content"]


# --- names without a shared room; busy/idle; rooms --------------------------------


async def test_a_stranger_is_named_by_the_nick_in_its_first_message(spawn_agent) -> None:
    """No shared room, no roster: XEP-0172's <nick/> still gives a name."""
    rev = await spawn_agent("rev", claude_name="Reviewer")
    bld = await spawn_agent("bld", claude_name="Builder")
    await bld.call("send_message", {"to": rev.jid, "body": "hello stranger"})
    ev = await rev.next_event()
    assert ev["meta"]["sender_jid"] == bld.jid        # canonical
    assert ev["meta"]["sender_name"] == "Builder"     # friendly
    await rev.call("reply", {"to": "Builder", "message": "hi Builder"})  # and addressable
    assert (await bld.next_event())["content"] == "hi Builder"


async def test_busy_and_idle_show_as_presence(spawn_agent, ejabberd: EjabberdHandle) -> None:
    room = _directory(ejabberd)
    rev = await spawn_agent("rev", "--join", room, claude_name="Reviewer")
    bld = await spawn_agent("bld", "--join", room, claude_name="Builder")
    for status, expected in (("busy", ("dnd", "busy")), ("idle", ("available", "idle"))):
        data = json.loads(rev.session_file.read_text())
        data["status"] = status
        rev.session_file.write_text(json.dumps(data))

        async def seen(expected=expected):
            agents = {a["address"]: a for a in (await bld.call("list_agents"))["agents"]}
            peer = agents.get(rev.jid)
            return peer and (peer["presence"], peer["status"]) == expected

        await _until(seen)
