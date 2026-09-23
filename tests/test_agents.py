"""Unit tests for agent presence metadata and the list_agents directory — no network."""

from __future__ import annotations

from typing import Any

import pytest
from slixmpp import Iq, Presence

from xmpp_mcp.agents import AGENT_NS, PresenceCache, best_presence, read_agent_info
from xmpp_mcp.config import Settings
from xmpp_mcp.xmpp_client import XMPPClient

ROOM = "agents@conference.xmpp.test"
MUC_USER = "http://jabber.org/protocol/muc#user"


def _client(**kw: Any) -> XMPPClient:
    return XMPPClient(Settings(  # type: ignore[call-arg]
        _env_file=None, xmpp_jid="bot@xmpp.test", xmpp_password="x", xmpp_nick="bot",
        **kw,
    ))


def _presence(
    c: XMPPClient,
    pfrom: str,
    ptype: str | None = None,
    show: str | None = None,
    status: str | None = None,
    agent: dict[str, str] | None = None,
    real_jid: str | None = None,
) -> Presence:
    pres = c.xmpp.make_presence(pfrom=pfrom, pto="bot@xmpp.test/r", pshow=show,
                                pstatus=status, ptype=ptype)
    if agent is not None:
        info = pres["mcp_agent"]
        for k, v in agent.items():
            info[k] = v
    if real_jid is not None:
        from xml.etree import ElementTree as ET

        x = ET.SubElement(pres.xml, f"{{{MUC_USER}}}x")
        ET.SubElement(x, f"{{{MUC_USER}}}item", {"jid": real_jid, "role": "participant",
                                                  "affiliation": "none"})
    return pres


# --- presence extension ------------------------------------------------------


async def test_outgoing_available_presence_is_stamped() -> None:
    c = _client(xmpp_agent_name="Rev", xmpp_agent_id="sess-1", xmpp_agent_host="host1")
    pres = c._stamp_agent_presence(c.xmpp.make_presence(pshow="away"))
    assert read_agent_info(pres) == {"id": "sess-1", "name": "Rev", "name-source": "",
                                     "host": "host1"}


@pytest.mark.parametrize("ptype", ["unavailable", "subscribe", "subscribed", "probe"])
async def test_non_availability_presence_is_not_stamped(ptype: str) -> None:
    c = _client(xmpp_agent_name="Rev")
    pres = c._stamp_agent_presence(c.xmpp.make_presence(ptype=ptype))
    assert pres.xml.find(f"{{{AGENT_NS}}}agent") is None


async def test_stamp_does_not_duplicate() -> None:
    c = _client(xmpp_agent_name="Rev")
    pres = c._stamp_agent_presence(c._stamp_agent_presence(c.xmpp.make_presence()))
    assert len(pres.xml.findall(f"{{{AGENT_NS}}}agent")) == 1


async def test_agent_feature_advertised_in_disco() -> None:
    c = _client()
    info = await c.xmpp.plugin["xep_0030"].get_info(local=True)
    assert AGENT_NS in info["features"]


# --- PresenceCache -----------------------------------------------------------


async def test_cache_tracks_and_evicts() -> None:
    c = _client()
    cache = PresenceCache()
    cache.update(_presence(c, "alice@xmpp.test/a", show="away", status="lunch",
                           agent={"id": "A1", "name": "Alice", "host": "h"}))
    entry = cache.get("alice@xmpp.test/a")
    assert entry is not None
    assert entry["show"] == "away" and entry["status"] == "lunch"
    assert entry["agent"] == {"id": "A1", "name": "Alice", "name-source": "", "host": "h"}
    cache.update(_presence(c, "alice@xmpp.test/a", ptype="unavailable"))
    assert cache.get("alice@xmpp.test/a") is None


async def test_cache_reads_muc_real_jid() -> None:
    c = _client()
    cache = PresenceCache()
    cache.update(_presence(c, f"{ROOM}/alice", real_jid="alice@xmpp.test/laptop"))
    assert cache.get(f"{ROOM}/alice")["real_jid"] == "alice@xmpp.test"  # type: ignore[index]


async def test_cache_ignores_subscription_presence() -> None:
    c = _client()
    cache = PresenceCache()
    cache.update(_presence(c, "alice@xmpp.test", ptype="subscribe"))
    assert cache.resources("alice@xmpp.test") == {}


def test_best_presence_prefers_most_available() -> None:
    entries = [{"show": "dnd"}, {"show": "chat"}, {"show": "away"}]
    assert best_presence(entries) == {"show": "chat"}
    assert best_presence([]) is None


# --- roster tracking ---------------------------------------------------------


async def test_roster_membership_follows_results_and_pushes() -> None:
    c = _client()
    iq = Iq(c.xmpp)
    iq["roster"]["items"] = {
        "alice@xmpp.test": {"subscription": "both", "name": "Alice"},
        "bob@xmpp.test": {"subscription": "none"},
    }
    c._on_roster_update(iq)
    assert c._roster_jids == {"alice@xmpp.test", "bob@xmpp.test"}
    push = Iq(c.xmpp)
    push["roster"]["items"] = {"bob@xmpp.test": {"subscription": "remove"}}
    c._on_roster_update(push)
    assert c._roster_jids == {"alice@xmpp.test"}


# --- list_agents -------------------------------------------------------------


def _fake_room(c: XMPPClient, monkeypatch: pytest.MonkeyPatch, occupants: dict[str, str | None]) -> None:
    """Pretend we're in ROOM with ``occupants`` (nick -> real JID or None)."""
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    monkeypatch.setattr(muc, "get_roster", lambda room: ["bot", *occupants] if room == ROOM else [])
    monkeypatch.setattr(
        muc, "get_jid_property",
        lambda room, nick, prop: occupants.get(nick) if prop == "jid" else None,
    )


async def test_list_agents_merges_roster_and_room(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._roster_jids = {"alice@xmpp.test", "carol@xmpp.test"}
    c.xmpp.client_roster["alice@xmpp.test"]["name"] = "Alice (roster)"
    _fake_room(c, monkeypatch, {"Alice": "alice@xmpp.test/a", "ghost": None})
    c.presence.update(_presence(c, "alice@xmpp.test/a",
                                agent={"id": "sess-A", "name": "Alice Agent", "host": "h1"}))
    c.presence.update(_presence(c, f"{ROOM}/Alice", real_jid="alice@xmpp.test/a"))
    c.presence.update(_presence(c, f"{ROOM}/ghost", show="xa"))

    agents = {a["address"]: a for a in c.list_agents()}
    assert set(agents) == {"alice@xmpp.test", "carol@xmpp.test", f"{ROOM}/ghost"}

    alice = agents["alice@xmpp.test"]
    assert alice["jid"] == "alice@xmpp.test"
    assert alice["agent_id"] == "sess-A"
    assert alice["name"] == "Alice Agent"  # self-advertised name wins over roster name
    assert alice["is_agent"] is True
    assert alice["presence"] == "available"
    assert alice["rooms"] == [{"room": ROOM, "nick": "Alice"}]  # merged, not duplicated

    ghost = agents[f"{ROOM}/ghost"]
    assert ghost["jid"] is None  # anonymous occupant: no canonical JID
    assert ghost["presence"] == "xa"

    assert agents["carol@xmpp.test"]["presence"] == "unavailable"


async def test_list_agents_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._roster_jids = {"carol@xmpp.test", "dave@xmpp.test"}
    c.presence.update(_presence(c, "dave@xmpp.test/x", agent={"id": "D", "name": "Dave"}))
    _fake_room(c, monkeypatch, {})
    assert [a["address"] for a in c.list_agents(include_offline=False)] == ["dave@xmpp.test"]
    assert [a["address"] for a in c.list_agents(agents_only=True)] == ["dave@xmpp.test"]
    # Online peers sort before offline ones.
    assert [a["address"] for a in c.list_agents()] == ["dave@xmpp.test", "carol@xmpp.test"]


async def test_list_agents_omits_self_and_rooms(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    c._roster_jids = {"bot@xmpp.test", ROOM}
    _fake_room(c, monkeypatch, {"me-elsewhere": "bot@xmpp.test/other"})
    assert c.list_agents() == []
