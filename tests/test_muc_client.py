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
from xmpp_mcp.xmpp_client import XMPPClient

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
    assert occupants == [{"nick": "alice", "role": "participant",
                          "affiliation": "member", "jid": "alice@xmpp.test/laptop"}]
    json.dumps(occupants)  # must not raise


async def test_room_occupants_without_disclosed_jid(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    monkeypatch.setattr(muc, "get_roster", lambda room: ["anon"])
    monkeypatch.setattr(muc, "get_jid_property", lambda room, nick, prop: None)
    assert c.room_occupants(ROOM) == [
        {"nick": "anon", "role": "", "affiliation": "", "jid": ""}
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
