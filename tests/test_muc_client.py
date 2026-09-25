"""Unit tests for MUC bookkeeping on :class:`XMPPClient` — no network required.

Room state is the part of the client that quietly goes wrong: occupant JIDs
that are not strings, a nick the room never agreed to, and room JIDs that are
canonical in one method and raw in the next.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from slixmpp import JID

from xmpp_mcp.config import Settings
from xmpp_mcp.xmpp_client import XMPPClient, XMPPError

ROOM = "agents@conference.xmpp.test"


def _client(**kw: Any) -> XMPPClient:
    return XMPPClient(Settings(  # type: ignore[call-arg]
        _env_file=None, xmpp_jid="bot@xmpp.test", xmpp_password="x", xmpp_nick="bot",
        **kw,
    ))


# --- MUC occupants -----------------------------------------------------------


async def test_room_occupants_are_json_serialisable(monkeypatch: pytest.MonkeyPatch) -> None:
    """slixmpp hands back a JID *object* for a disclosed real JID.

    Returned as-is it isn't JSON-serialisable, so FastMCP silently drops the
    tool's structured content (and a strict client then errors).
    """
    c = _client()
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    props = {"jid": JID("alice@xmpp.test/laptop"), "role": "participant",
             "affiliation": "member"}
    monkeypatch.setattr(muc, "get_roster", lambda room: ["alice"])
    monkeypatch.setattr(muc, "get_jid_property", lambda room, nick, prop: props.get(prop))

    occupants = c.room_occupants(ROOM)
    assert occupants == [{"nick": "alice", "role": "participant", "affiliation": "member",
                          "jid": "alice@xmpp.test/laptop", "me": False}]
    json.dumps(occupants)  # must not raise


async def test_room_occupants_without_disclosed_jid(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    monkeypatch.setattr(muc, "get_roster", lambda room: ["anon"])
    monkeypatch.setattr(muc, "get_jid_property", lambda room, nick, prop: None)
    assert c.room_occupants(ROOM) == [
        {"nick": "anon", "role": "", "affiliation": "", "jid": "", "me": False}
    ]


# --- room bookkeeping --------------------------------------------------------


async def test_joined_nick_follows_the_room_not_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The room decides the nick (XEP-0045 §7.2.9, and prep folds ours).

    Anything that has to recognise our own traffic compares against this
    value, so a stale nick is how a client stops recognising itself.
    """
    c = _client()
    muc = c.xmpp.plugin["xep_0045"]

    async def fake_join(room, nick, **kw):
        # The service accepts us under a different nick than we asked for.
        muc.our_nicks.setdefault(None, {})[room] = "assigned-nick"

    monkeypatch.setattr(muc, "join_muc_wait", fake_join)
    monkeypatch.setattr(muc, "get_roster", lambda room: [])
    result = await c.join_room(ROOM, "requested-nick")
    assert result["nick"] == "assigned-nick"
    assert c.nick_in(ROOM) == "assigned-nick"


async def test_nick_change_by_the_room_is_tracked() -> None:
    """XEP-0045 §7.6: status 303 renames us; the filter must follow."""
    c = _client()
    c._joined_rooms[ROOM] = "old-nick"
    pres = c.xmpp.make_presence(pfrom=f"{ROOM}/old-nick", pto="bot@xmpp.test/r")
    pres["muc"]["status_codes"] = {110, 303}
    pres["muc"]["item_nick"] = "new-nick"
    c._on_muc_self_presence(pres)
    assert c.nick_in(ROOM) == "new-nick"


async def test_another_occupants_presence_does_not_change_our_nick() -> None:
    c = _client()
    c._joined_rooms[ROOM] = "bot"
    pres = c.xmpp.make_presence(pfrom=f"{ROOM}/alice", pto="bot@xmpp.test/r")
    c._on_muc_self_presence(pres)  # no status code 110
    assert c.nick_in(ROOM) == "bot"


async def test_room_jids_are_canonicalised_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """join_room() preps the JID, so every other room call must prep it too."""
    c = _client()
    muc = c.xmpp.plugin["xep_0045"]

    async def fake_join(room, nick, **kw):
        muc.our_nicks.setdefault(None, {})[room] = nick

    monkeypatch.setattr(muc, "join_muc_wait", fake_join)
    monkeypatch.setattr(muc, "get_roster", lambda room: [])
    monkeypatch.setattr(muc, "leave_muc", lambda room, nick: None)

    mixed = "Probe@Conference.XMPP.test"
    joined = await c.join_room(mixed, "bot")
    assert joined["room"] == "probe@conference.xmpp.test"
    assert c.is_joined(mixed) and c.nick_in(mixed) == "bot"
    c.send_groupchat(mixed, "hello")  # must not raise "Not joined"
    assert c.room_occupants(mixed) == []
    c.leave_room(mixed)
    assert not c.is_joined(mixed)


async def test_a_failed_attempt_is_not_final_without_a_pinned_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slixmpp fires connection_failed per attempt, not once overall.

    With no pinned host and no SRV records it probes the domain with direct TLS
    on 5222 first, then STARTTLS; giving up on the first failure meant no agent
    could reach a server by its domain alone.
    """
    c = _client()

    def connect() -> None:
        c._on_connection_failed("[SSL: WRONG_VERSION_NUMBER]")  # the direct-TLS probe
        c._ready.set_result(True)  # ... then STARTTLS succeeds

    monkeypatch.setattr(c, "_connect", connect)
    await c.start()


async def test_a_pinned_host_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """With XMPP_HOST there is one attempt per round: its failure is the answer."""
    c = _client(xmpp_host="127.0.0.1")
    monkeypatch.setattr(c, "_connect", lambda: c._on_connection_failed("refused"))
    with pytest.raises(XMPPError, match="Connection failed: refused"):
        await c.start()


async def test_a_timeout_names_the_last_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(xmpp_connect_timeout=0.2)
    monkeypatch.setattr(c, "_connect", lambda: c._on_connection_failed("no route to host"))
    with pytest.raises(XMPPError, match="last connection error: no route to host"):
        await c.start()


async def test_disconnect_after_a_start_timeout_is_quiet() -> None:
    """wait_for() cancels the readiness future; .exception() would then raise."""
    c = _client()
    c._ready.cancel()
    c._on_disconnected("stream closed")  # must not raise CancelledError


async def test_a_room_is_remembered_after_leaving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Knowing a JID is a room (not a person) is what stops `reply` sending a
    1:1 chat to a bare room JID, which the service just drops."""
    c = _client()
    muc = c.xmpp.plugin["xep_0045"]

    async def fake_join(room, nick, **kw):
        muc.our_nicks.setdefault(None, {})[room] = nick

    monkeypatch.setattr(muc, "join_muc_wait", fake_join)
    monkeypatch.setattr(muc, "get_roster", lambda room: [])
    monkeypatch.setattr(muc, "leave_muc", lambda room, nick: None)

    await c.join_room("Probe@Conference.XMPP.test", "bot")
    c.leave_room("Probe@Conference.XMPP.test")
    assert not c.is_joined("probe@conference.xmpp.test")
    assert c.is_known_room("Probe@Conference.XMPP.test")  # canonicalised too


async def test_a_confirmed_rename_refreshes_our_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a 303, re-announce under the new nick.

    ejabberd re-broadcasts the presence we *joined* with under the new nick,
    so without this, occupants keep seeing the old <agent name>.
    """
    c = _client()
    c._joined_rooms[ROOM] = "old-nick"
    sent: list[str | None] = []
    monkeypatch.setattr(c.xmpp, "send_presence", lambda pto=None, **kw: sent.append(pto))
    pres = c.xmpp.make_presence(pfrom=f"{ROOM}/old-nick", pto="bot@xmpp.test/r")
    pres["muc"]["status_codes"] = {110, 303}
    pres["muc"]["item_nick"] = "new-nick"
    c._on_muc_self_presence(pres)
    assert sent == [f"{ROOM}/new-nick"]


# --- rooms: discovery and members ---------------------------------------------


def _room_info_iq(c: XMPPClient, features: list[str], fields: dict[str, str], name: str) -> Any:
    iq = c.xmpp.Iq()
    info = iq["disco_info"]
    info.add_identity("conference", "text", name=name)
    for f in features:
        info.add_feature(f)
    form = c.xmpp.plugin["xep_0004"].make_form(ftype="result")
    form.add_field(var="FORM_TYPE", ftype="hidden", value="http://jabber.org/protocol/muc#roominfo")
    for var, value in fields.items():
        form.add_field(var=var, value=value)
    info.append(form)
    return iq


async def test_room_info_is_read_from_disco(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()

    async def get_info(jid: str, cached: bool = False) -> Any:
        return _room_info_iq(
            c, ["muc_public", "muc_nonanonymous", "muc_persistent"],
            {"muc#roominfo_description": "Where the agents meet",
             "muc#roominfo_occupants": "4", "muc#roominfo_subject": "standup"},
            name="Agents",
        )

    monkeypatch.setattr(c.xmpp.plugin["xep_0030"], "get_info", get_info)
    info = await c._room_info(ROOM)
    assert info == {
        "public": True, "members_only": False, "password_protected": False,
        "anonymous": False, "persistent": True, "name": "Agents",
        "description": "Where the agents meet", "occupants": 4, "subject": "standup",
    }


async def test_list_rooms_includes_joined_rooms(monkeypatch: pytest.MonkeyPatch) -> None:
    """disco lists public rooms only; the ones we are in must still show."""
    c = _client()
    c._joined_rooms["hidden@conference.xmpp.test"] = "bot"

    async def disco_items(jid: str | None = None) -> list[dict[str, str]]:
        return [{"jid": ROOM, "node": "", "name": "Agents"}]

    async def room_info(room: str) -> dict[str, Any]:
        return {"occupants": 2}

    monkeypatch.setattr(c, "disco_items", disco_items)
    monkeypatch.setattr(c, "_room_info", room_info)
    got = await c.list_rooms("conference.xmpp.test")
    by_room = {r["room"]: r for r in got["rooms"]}
    assert set(by_room) == {ROOM, "hidden@conference.xmpp.test"}
    assert by_room[ROOM]["joined"] is False and by_room[ROOM]["name"] == "Agents"
    assert by_room["hidden@conference.xmpp.test"]["joined"] is True
    assert by_room["hidden@conference.xmpp.test"]["nick"] == "bot"


async def test_joined_room_occupants_carry_agent_details(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    monkeypatch.setattr(muc, "get_roster", lambda room: ["bot", "Reviewer"])
    monkeypatch.setattr(muc, "get_jid_property", lambda room, nick, prop: None)
    pres = c.xmpp.make_presence(pfrom=f"{ROOM}/Reviewer", pto="bot@xmpp.test/r", pshow="dnd",
                                pstatus="busy")
    info = pres["mcp_agent"]
    info["id"], info["name"] = "sess-r", "Reviewer"
    c.presence.update(pres)
    occ = {o["nick"]: o for o in c.room_occupants(ROOM)}
    assert occ["bot"]["me"] is True
    assert (occ["Reviewer"]["name"], occ["Reviewer"]["agent_id"]) == ("Reviewer", "sess-r")
    assert (occ["Reviewer"]["presence"], occ["Reviewer"]["status"]) == ("dnd", "busy")


async def test_members_of_a_room_we_are_not_in(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()

    async def disco_items(jid: str | None = None) -> list[dict[str, str]]:
        return [{"jid": f"{ROOM}/alice", "node": "", "name": ""},
                {"jid": f"{ROOM}/Reviewer", "node": "", "name": ""}]

    async def room_info(room: str) -> dict[str, Any]:
        return {"occupants": 2}

    monkeypatch.setattr(c, "disco_items", disco_items)
    monkeypatch.setattr(c, "_room_info", room_info)
    got = await c.room_members(ROOM)
    assert got == {"room": ROOM, "joined": False, "occupant_count": 2,
                   "occupants": [{"nick": "alice"}, {"nick": "Reviewer"}]}


async def test_a_hidden_occupant_list_is_not_an_empty_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XEP-0045 §6.5 lets a service keep the list from non-members (Prosody does)."""
    c = _client()

    async def nobody(jid: str | None = None) -> list[dict[str, str]]:
        return []

    async def room_info(room: str) -> dict[str, Any]:
        return {"occupants": 3}

    monkeypatch.setattr(c, "disco_items", nobody)
    monkeypatch.setattr(c, "_room_info", room_info)
    got = await c.room_members(ROOM)
    assert (got["occupants"], got["hidden"], got["occupant_count"]) == ([], True, 3)


async def test_a_room_that_hides_its_members(monkeypatch: pytest.MonkeyPatch) -> None:
    from xmpp_mcp.xmpp_client import XMPPError

    c = _client()

    async def refuse(jid: str | None = None) -> list[dict[str, str]]:
        raise XMPPError("forbidden")

    monkeypatch.setattr(c, "disco_items", refuse)
    with pytest.raises(XMPPError, match="join_room to see them"):
        await c.room_members(ROOM)


async def test_a_room_is_joined_with_our_busy_or_idle_state(monkeypatch) -> None:
    """Regression: the join presence carried no show/status, and busy/idle
    only re-announces on a change, so the room (and the relay's directory)
    saw neither busy nor idle until the session's state next changed."""
    c = _client()
    muc = c.xmpp.plugin["xep_0045"]
    seen: list = []

    async def fake_join(room, nick, **kw):
        seen.append(kw.get("presence_options"))

    monkeypatch.setattr(muc, "join_muc_wait", fake_join)
    monkeypatch.setattr(muc, "get_roster", lambda room: [])
    c._presence = (None, "idle")
    await c.join_room(ROOM, "me")
    c._presence = ("dnd", "busy")
    await c.join_room("other@conference.xmpp.test", "me")
    c._presence = (None, None)
    await c.join_room("third@conference.xmpp.test", "me")
    assert seen == [{"pstatus": "idle"}, {"pshow": "dnd", "pstatus": "busy"}, None]
