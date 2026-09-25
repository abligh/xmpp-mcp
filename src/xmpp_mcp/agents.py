"""Agent metadata carried in presence, and the peer directory built from it.

Every available presence this server sends carries a small extension element
(RFC 6120 §8.4 allows any namespaced child) describing the agent behind the
JID::

    <presence>
      <agent xmlns="urn:xmpp-mcp:agent:0"
             id="4f0c…"            <!-- internal ID (Claude Code session ID) -->
             name="Reviewer"       <!-- friendly, human-facing name -->
             name-source="auto"    <!-- how that name came about (see
                                        claude_session.NAME_SOURCES) -->
             host="host1"/>        <!-- where the agent runs -->
    </presence>

Presence is the natural carrier: it already reaches every roster contact
(RFC 6121 §4) and every occupant of a shared room (XEP-0045 §7.2), it is
re-sent on every reconnect, and it is withdrawn automatically when the agent
goes offline. Peers that don't understand the namespace ignore it.

:class:`PresenceCache` remembers the last available presence per full JID
(including MUC occupant JIDs) so ``list_agents`` can report who is online
without a round-trip. Values are **self-asserted** by the peer: the channel's
sender gate uses the server-authenticated JID, never these attributes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from slixmpp import JID, Presence
from slixmpp.xmlstream import ElementBase

AGENT_NS = "urn:xmpp-mcp:agent:0"
# XEP-0319 (Last User Interaction in Presence): when an idle agent went idle.
IDLE_NS = "urn:xmpp:idle:1"

# Presence types that describe availability. Everything else (unavailable,
# subscription management, probes, errors) must not carry agent metadata.
_AVAILABLE_TYPES = frozenset({"available", "chat", "away", "dnd", "xa"})


class AgentInfo(ElementBase):
    """``<agent xmlns="urn:xmpp-mcp:agent:0" id=… name=… name-source=… host=…/>``."""

    name = "agent"
    namespace = AGENT_NS
    plugin_attrib = "mcp_agent"
    interfaces = {"id", "name", "name-source", "host"}


def is_available(pres: Presence) -> bool:
    """True for presence stanzas that announce availability (RFC 6121 §4.7.1)."""
    return pres["type"] in _AVAILABLE_TYPES


def read_agent_info(pres: Presence) -> dict[str, str] | None:
    """Extract the agent extension from ``pres`` as a dict, or ``None`` if absent."""
    el = pres.xml.find(f"{{{AGENT_NS}}}agent")
    if el is None:
        return None
    return {k: el.get(k, "") for k in ("id", "name", "name-source", "host")}


def idle_stamp(when: datetime) -> str:
    """XEP-0082 DateTime, UTC with a Z, whole seconds: XEP-0319's ``since``."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_idle_since(pres: Presence) -> str | None:
    """The XEP-0319 ``since`` on ``pres``, normalised to UTC, or ``None``.

    Self-asserted by the peer, like the agent extension, and dropped if it
    doesn't parse: a malformed stamp must not become a date.
    """
    el = pres.xml.find(f"{{{IDLE_NS}}}idle")
    raw = el.get("since") if el is not None else None
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None  # XEP-0082 requires a zone; guessing one would mislead
    return idle_stamp(when)


class PresenceCache:
    """Last known available presence per full JID.

    Keys are full JIDs as strings: ``user@domain/resource`` for direct
    contacts, ``room@service/nick`` for MUC occupants. An ``unavailable``
    presence evicts the entry, so the cache always reflects who is online now.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def update(self, pres: Presence) -> None:
        """Record (or evict, for ``unavailable``) the sender of ``pres``."""
        key = pres["from"].full
        if pres["type"] == "unavailable":
            self._entries.pop(key, None)
            return
        if not is_available(pres):
            return
        real_jid: str | None = None
        # XEP-0045 §7.2.3: non-anonymous rooms (and moderators in
        # semi-anonymous rooms) see each occupant's real JID in muc#user.
        muc_item = pres.xml.find("{http://jabber.org/protocol/muc#user}x/"
                                 "{http://jabber.org/protocol/muc#user}item")
        if muc_item is not None and muc_item.get("jid"):
            real_jid = JID(muc_item.get("jid")).bare
        self._entries[key] = {
            "show": pres["show"] or "available",
            "status": pres["status"] or "",
            "agent": read_agent_info(pres),
            "real_jid": real_jid,
            "idle_since": read_idle_since(pres),
        }

    def get(self, full_jid: str) -> dict[str, Any] | None:
        return self._entries.get(full_jid)

    def resources(self, bare_jid: str) -> dict[str, dict[str, Any]]:
        """All cached entries whose bare JID is ``bare_jid`` (resource → entry)."""
        prefix = bare_jid + "/"
        return {
            k[len(prefix):]: v for k, v in self._entries.items() if k.startswith(prefix)
        }

    def agent_name_of(self, bare_jid: str) -> str | None:
        """The friendly name a peer advertises, looked up by its real bare JID.

        A peer is seen either directly (roster presence, keyed by its own full
        JID) or through a room it shares with us (keyed by occupant JID, with
        the real JID in ``real_jid``). Either will do.
        """
        prefix = bare_jid + "/"
        for key, entry in self._entries.items():
            agent = entry.get("agent")
            if not agent or not agent.get("name"):
                continue
            if key.startswith(prefix) or entry.get("real_jid") == bare_jid:
                return agent["name"]
        return None

    def clear(self) -> None:
        self._entries.clear()


# RFC 6121 §4.7.2.1 ordering: most to least available. Used to pick the
# "headline" presence of a contact that is online from several resources.
_SHOW_RANK = {"chat": 0, "available": 1, "away": 2, "xa": 3, "dnd": 4}


def best_presence(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most available of several presence entries (or ``None`` if empty)."""
    if not entries:
        return None
    return min(entries, key=lambda e: _SHOW_RANK.get(e["show"], 99))
