"""slixmpp-based XMPP client wrapper used by all messaging/MUC/presence tools.

Owns a single ``ClientXMPP`` connection for the life of the MCP server. The
connection is opened in the FastMCP lifespan and shared with every tool via the
lifespan context. Inbound messages are buffered in a bounded deque because the
MCP model is request/response — tools pull messages, the server does not push.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections import deque
from datetime import datetime, timezone
from typing import Any

from slixmpp import JID, ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout, PresenceError
from slixmpp.jid import InvalidJID

from .config import Settings
from .security_labels import SEC_LABEL_NS

logger = logging.getLogger("xmpp_mcp.xmpp")


class XMPPError(RuntimeError):
    """Raised when an XMPP operation fails in a way worth surfacing to the caller."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def room_key(room: str) -> str:
    """Canonical key for a room: the prepped, case-folded bare JID.

    Room JIDs compare case-insensitively (RFC 7622), so every room API has to
    agree on one spelling — otherwise ``join_room("Room@Conf.Example")``
    succeeds while every later call with the same string reports "not joined".
    """
    return parse_jid(room, "room JID").bare


def parse_jid(value: str, what: str = "JID") -> JID:
    """Parse ``value`` as a JID (RFC 7622), raising :class:`XMPPError` if invalid."""
    try:
        jid = JID(value)
    except InvalidJID as exc:
        raise XMPPError(f"Invalid {what} {value!r}: {exc}") from exc
    if not jid.domain:
        raise XMPPError(f"Invalid {what} {value!r}: missing domain")
    return jid


def _read_displaymarking(msg: Any) -> str | None:
    """Extract the XEP-0258 display marking text from a message, if present."""
    el = msg.xml.find(f"{{{SEC_LABEL_NS}}}securitylabel/{{{SEC_LABEL_NS}}}displaymarking")
    if el is not None and el.text:
        return el.text.strip()
    return None


class XMPPClient:
    """A thin, async wrapper around ``slixmpp.ClientXMPP``.

    Lifecycle: ``await start()`` once, use the helper methods, ``await stop()``
    on shutdown. All helpers raise :class:`XMPPError` on failure so tool code can
    convert that into a clean MCP error.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.xmpp = ClientXMPP(settings.xmpp_jid, settings.xmpp_password)
        # Constructed inside the FastMCP lifespan, so a loop is always running.
        self._ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._inbox: deque[dict[str, Any]] = deque(maxlen=settings.xmpp_inbox_size)
        self._joined_rooms: dict[str, str] = {}  # room bare JID -> nick in use
        # Pubsub notifications: bounded buffer that captures publish / retract /
        # purge / delete / config / subscription events on nodes we're subscribed
        # to. ``_seen_pubsub_msg_ids`` dedupes slixmpp's per-item event firing.
        self._pubsub_events: deque[dict[str, Any]] = deque(
            maxlen=settings.xmpp_inbox_size
        )
        self._seen_pubsub_msg_ids: deque[str] = deque(maxlen=200)

        # XEPs every tool surface depends on. xep_0258 (security labels) is
        # registered separately in start() because it is our own plugin.
        # xep_0004 (data forms) and xep_0060 (pubsub) power the pubsub tools.
        for xep in (
            "xep_0030",
            "xep_0045",
            "xep_0199",
            "xep_0203",
            "xep_0004",
            "xep_0060",
            "xep_0313",  # MAM — historical room queries via the Monitoring plugin
        ):
            self.xmpp.register_plugin(xep)

        if settings.xmpp_host:
            # When the caller pins an explicit host/port, slixmpp's default of
            # *also* attempting direct TLS on that same port produces a confusing
            # failure mode on the STARTTLS port (5222) — the TLS ClientHello is
            # parsed as XML by the server and the stream is reset before we
            # ever try STARTTLS. Pin to STARTTLS only.
            self.xmpp.enable_direct_tls = False

        if settings.xmpp_tls_insecure:
            logger.warning("XMPP_TLS_INSECURE is set — TLS certificate checks disabled")
            self.xmpp.ssl_context.check_hostname = False
            self.xmpp.ssl_context.verify_mode = ssl.CERT_NONE
            # Lab servers (e.g. ejabberd without auto-generated certs) may not
            # offer STARTTLS at all. slixmpp refuses to send SASL over a plain
            # stream by default — opt in for lab use only.
            try:
                mechs = self.xmpp["feature_mechanisms"]
                mechs.unencrypted_plain = True
                mechs.unencrypted_scram = True
            except KeyError:
                pass

        self.xmpp.add_event_handler("session_start", self._on_session_start)
        # `failed_auth` fires after **each** failed SASL mechanism — slixmpp may
        # then try the next one. Wait for `failed_all_auth`, which fires only
        # after the whole mechanism list is exhausted. (Without this distinction,
        # one server's SCRAM-PLUS quirk failing the first attempt would tear
        # down the session even though PLAIN was about to succeed.)
        self.xmpp.add_event_handler("failed_all_auth", self._on_failed_auth)
        self.xmpp.add_event_handler("connection_failed", self._on_connection_failed)
        # ``message`` fires for *every* incoming message stanza — including
        # groupchat. ``groupchat_message`` is an additional, narrower event
        # raised by xep_0045 for the same stanza, so subscribing to both
        # doubles every MUC line in the inbox. Subscribe once.
        self.xmpp.add_event_handler("message", self._on_message)
        # The room, not us, decides our nick (XEP-0045 §7.2.9/§7.6).
        self.xmpp.add_event_handler("groupchat_presence", self._on_muc_self_presence)
        # Pubsub notification events. slixmpp fires `pubsub_publish` /
        # `pubsub_retract` once *per item*; the handler dedupes by msg id and
        # walks every item in one pass.
        for ev in ("pubsub_publish", "pubsub_retract"):
            self.xmpp.add_event_handler(ev, self._on_pubsub_items)
        self.xmpp.add_event_handler("pubsub_purge", self._on_pubsub_purge)
        self.xmpp.add_event_handler("pubsub_delete", self._on_pubsub_delete)
        self.xmpp.add_event_handler("pubsub_config", self._on_pubsub_config)
        self.xmpp.add_event_handler(
            "pubsub_subscription", self._on_pubsub_subscription
        )

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Connect and block until the XMPP session is established."""
        s = self._settings
        if s.xmpp_host:
            self.xmpp.connect(host=s.xmpp_host, port=s.xmpp_port)
        else:
            self.xmpp.connect()
        try:
            await asyncio.wait_for(self._ready, timeout=s.xmpp_connect_timeout)
        except asyncio.TimeoutError as exc:
            raise XMPPError(
                f"Timed out after {s.xmpp_connect_timeout}s establishing the XMPP session"
            ) from exc
        logger.info("XMPP session established as %s", self.xmpp.boundjid.full)

    async def stop(self) -> None:
        """Leave joined rooms and disconnect cleanly."""
        for room, nick in list(self._joined_rooms.items()):
            try:
                self.xmpp.plugin["xep_0045"].leave_muc(room, nick)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Failed to leave room %s during shutdown", room, exc_info=True)
        self._joined_rooms.clear()
        try:
            await self.xmpp.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("Error during XMPP disconnect", exc_info=True)
        logger.info("XMPP connection closed")

    # --- event handlers -------------------------------------------------------

    async def _on_session_start(self, _event: Any) -> None:
        try:
            self.xmpp.send_presence()
            await self.xmpp.get_roster()
        except (IqError, IqTimeout) as exc:
            if not self._ready.done():
                self._ready.set_exception(XMPPError(f"Roster fetch failed on login: {exc}"))
            return
        if not self._ready.done():
            self._ready.set_result(True)

    def _on_failed_auth(self, _event: Any) -> None:
        if not self._ready.done():
            self._ready.set_exception(
                XMPPError("Authentication failed — check XMPP_JID and XMPP_PASSWORD")
            )

    def _on_connection_failed(self, reason: Any) -> None:
        if not self._ready.done():
            self._ready.set_exception(XMPPError(f"Connection failed: {reason}"))

    def _on_message(self, msg: Any) -> None:
        # Direct chat/normal stanzas and groupchat stanzas both land here.
        if msg["type"] in ("chat", "normal", "groupchat") and msg["body"]:
            is_muc = msg["type"] == "groupchat"
            self._inbox.append(
                {
                    "from": msg["from"].full,
                    "to": msg["to"].full,
                    "type": msg["type"],
                    "body": msg["body"],
                    "security_label": _read_displaymarking(msg),
                    "timestamp": _now_iso(),
                    # For groupchat the JID splits into room (.bare) and the
                    # speaker's MUC nick (.resource). For 1:1 these stay None
                    # so tools/search treat them uniformly.
                    "room": msg["from"].bare if is_muc else None,
                    "nick": msg["from"].resource if is_muc else None,
                }
            )

    def _on_muc_self_presence(self, pres: Any) -> None:
        """Keep ``_joined_rooms`` in step with the nick the room gives us.

        Status code 110 marks our own presence (XEP-0045 §7.2.3); 303 means
        the room renamed us, in which case the new nick is in the item. Our
        self-echo filter compares against this value.
        """
        codes = pres["muc"]["status_codes"]
        if 110 not in codes:
            return
        room = pres["from"].bare
        if room not in self._joined_rooms:
            return
        nick = pres["muc"]["item_nick"] if 303 in codes else pres["from"].resource
        if nick and nick != self._joined_rooms[room]:
            logger.info("Nick in %s is now %r (was %r)", room, nick, self._joined_rooms[room])
            self._joined_rooms[room] = nick

    # --- messaging ------------------------------------------------------------

    def send_chat(self, to: str, body: str, label: Any = None) -> None:
        """Send a 1:1 chat message. ``label`` is an optional raw XEP-0258 element."""
        msg = self.xmpp.make_message(mto=to, mbody=body, mtype="chat")
        if label is not None:
            msg.appendxml(label)
        msg.send()

    def send_groupchat(self, room: str, body: str, label: Any = None) -> None:
        """Send a message to a MUC room. ``label`` is an optional raw XEP-0258 element."""
        room = room_key(room)
        if room not in self._joined_rooms:
            raise XMPPError(f"Not joined to room {room} — call join_room first")
        msg = self.xmpp.make_message(mto=room, mbody=body, mtype="groupchat")
        if label is not None:
            msg.appendxml(label)
        msg.send()

    def search_inbox(
        self,
        query: str | None = None,
        room: str | None = None,
        participant: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Non-destructively scan the inbound buffer.

        ``query`` is a case-insensitive substring of the body. ``room`` matches
        the room bare JID for MUC messages. ``participant`` is flexible: a value
        containing ``@`` matches the sender's bare JID (1:1 chats); a bare nick
        matches the MUC ``resource`` of any joined-room message. ``since`` is
        an ISO timestamp lower bound.
        """
        needle = query.lower() if query else None
        results: list[dict[str, Any]] = []
        # Newest-first — more useful when the buffer is large.
        for item in reversed(self._inbox):
            if needle and needle not in item["body"].lower():
                continue
            if room and item.get("room") != room:
                continue
            if participant:
                if "@" in participant:
                    bare = item["from"].split("/", 1)[0]
                    if bare != participant:
                        continue
                else:
                    # Match the MUC nick (resource). Skips 1:1 chats whose
                    # resource is a generated client tag rather than a name.
                    if item.get("nick") != participant:
                        continue
            if since and item["timestamp"] < since:
                continue
            results.append(dict(item))  # shallow copy: don't share mutable state
            if len(results) >= limit:
                break
        return results

    def drain_inbox(self, from_jid: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return and remove buffered inbound messages (newest last).

        If ``from_jid`` is given, only messages whose sender's bare JID matches
        are drained; others stay in the buffer.
        """
        if from_jid is None:
            picked = list(self._inbox)[-limit:]
            for item in picked:
                self._inbox.remove(item)
            return picked

        matched: list[dict[str, Any]] = []
        for item in list(self._inbox):
            if item["from"].split("/")[0] == from_jid:
                matched.append(item)
                self._inbox.remove(item)
                if len(matched) >= limit:
                    break
        return matched

    # --- presence & roster ----------------------------------------------------

    def set_presence(self, show: str | None = None, status: str | None = None) -> None:
        """Update the bot's presence. ``show`` is one of chat/away/dnd/xa or None.

        The broadcast (RFC 6121 §4.4) only reaches roster contacts; room
        occupants see a change only if it is also sent as directed presence
        to our occupant JID in each room (XEP-0045 §7.7).
        """
        self.xmpp.send_presence(pshow=show, pstatus=status)
        for room, nick in self._joined_rooms.items():
            self.xmpp.send_presence(pto=f"{room}/{nick}", pshow=show, pstatus=status)

    def get_roster(self) -> list[dict[str, Any]]:
        """Return the current roster as a list of contact dicts."""
        roster = self.xmpp.client_roster
        contacts: list[dict[str, Any]] = []
        for jid in roster:
            item = roster[jid]
            contacts.append(
                {
                    "jid": jid,
                    "name": item["name"] or "",
                    "subscription": item["subscription"],
                    "groups": list(item["groups"] or []),
                }
            )
        return contacts

    async def add_contact(self, jid: str, name: str | None = None) -> None:
        """Add a roster entry and send a subscription request."""
        try:
            self.xmpp.send_presence_subscription(pto=jid)
            await self.xmpp.client_roster.update(jid, name=name, subscription="both")
        except (IqError, IqTimeout) as exc:
            raise XMPPError(f"Failed to add contact {jid}: {exc}") from exc

    async def remove_contact(self, jid: str) -> None:
        """Remove a roster entry and unsubscribe."""
        try:
            await self.xmpp.client_roster.remove(jid)
        except (IqError, IqTimeout) as exc:
            raise XMPPError(f"Failed to remove contact {jid}: {exc}") from exc

    # --- MUC ------------------------------------------------------------------

    async def join_room(self, room: str, nick: str | None = None) -> dict[str, Any]:
        """Join a MUC room. Returns the room JID, nick used, and current occupants.

        ``maxstanzas=0`` opts out of MUC history-on-join — old room chatter
        should not back-fill the bot's inbox and skew ``search_messages``.
        Tools that want history can query a MAM archive explicitly.
        """
        nick = nick or self._settings.xmpp_nick
        room = room_key(room)
        muc = self.xmpp.plugin["xep_0045"]
        try:
            await muc.join_muc_wait(
                room,
                nick,
                maxstanzas=0,
                timeout=self._settings.xmpp_connect_timeout,
            )
        except (PresenceError, IqError, IqTimeout, asyncio.TimeoutError) as exc:
            # PresenceError covers the MUC-level refusals: nick conflict,
            # members-only, banned, password required (XEP-0045 §7.2.x).
            raise XMPPError(f"Failed to join room {room}: {exc}") from exc
        # The room decides the nick, and it need not be the one we asked for
        # (§7.2.9 lets the service assign one, and prep can fold ours). Trust
        # the nick in the self-presence: the self-echo filter compares against
        # it, so a stale value would push our own messages back at us.
        self._joined_rooms[room] = muc.our_nicks.get(None, {}).get(room, nick)
        return {
            "room": room,
            "nick": self._joined_rooms[room],
            "occupants": self.room_occupants(room),
        }

    def leave_room(self, room: str) -> None:
        """Leave a previously joined MUC room."""
        room = room_key(room)
        nick = self._joined_rooms.pop(room, None)
        if nick is None:
            raise XMPPError(f"Not joined to room {room}")
        self.xmpp.plugin["xep_0045"].leave_muc(room, nick)

    def room_occupants(self, room: str) -> list[dict[str, Any]]:
        """Return occupants of a joined room with role/affiliation where known."""
        room = room_key(room)
        if room not in self._joined_rooms:
            raise XMPPError(f"Not joined to room {room} — call join_room first")
        muc = self.xmpp.plugin["xep_0045"]
        occupants: list[dict[str, Any]] = []
        for nick in muc.get_roster(room):
            # get_jid_property returns a slixmpp JID object for "jid" (only in
            # rooms that disclose real JIDs) — stringify it, or the tool result
            # is not JSON-serialisable and loses its structured content.
            real = muc.get_jid_property(room, nick, "jid")
            occupants.append(
                {
                    "nick": nick,
                    "role": muc.get_jid_property(room, nick, "role") or "",
                    "affiliation": muc.get_jid_property(room, nick, "affiliation") or "",
                    "jid": str(real) if real else "",
                }
            )
        return occupants

    def is_joined(self, room: str) -> bool:
        return room_key(room) in self._joined_rooms

    def nick_in(self, room: str) -> str | None:
        """Our nick in ``room``, or ``None`` if not joined."""
        return self._joined_rooms.get(room_key(room))

    @property
    def joined_rooms(self) -> list[str]:
        return list(self._joined_rooms)

    # --- pubsub event capture -------------------------------------------------

    def _record_event(self, event: dict[str, Any]) -> None:
        event["timestamp"] = _now_iso()
        self._pubsub_events.append(event)

    def _on_pubsub_items(self, msg: Any) -> None:
        """Capture ``pubsub_publish`` / ``pubsub_retract`` notifications.

        slixmpp fires the event once per item, with the *full* msg attached on
        every fire — so we dedupe by msg id and process all items in one pass.
        """
        msg_id = msg["id"] or ""
        if msg_id and msg_id in self._seen_pubsub_msg_ids:
            return
        if msg_id:
            self._seen_pubsub_msg_ids.append(msg_id)

        # Lazy imports — security_labels is already imported, but data_forms
        # is only needed for pubsub-event payload parsing.
        from .data_forms import NS as DATA_FORMS_NS, parse_form

        service = str(msg["from"])
        try:
            node = msg["pubsub_event"]["items"]["node"]
        except Exception:  # noqa: BLE001
            node = ""

        from xml.etree import ElementTree as etree

        for item in msg["pubsub_event"]["items"]:
            if item.name == "item":
                kind = "publish"
                payload = item["payload"]
                form = None
                payload_xml: str | None = None
                if payload is not None:
                    if payload.tag == f"{{{DATA_FORMS_NS}}}x":
                        try:
                            form = parse_form(payload)
                        except Exception:  # noqa: BLE001
                            form = None
                    if form is None:
                        payload_xml = etree.tostring(payload, encoding="unicode")
                self._record_event(
                    {
                        "kind": kind,
                        "service": service,
                        "node": node,
                        "item_id": item["id"] or None,
                        "form": form,
                        "payload_xml": payload_xml,
                    }
                )
            elif item.name == "retract":
                self._record_event(
                    {
                        "kind": "retract",
                        "service": service,
                        "node": node,
                        "item_id": item["id"] or None,
                    }
                )

    def _on_pubsub_purge(self, msg: Any) -> None:
        self._record_event(
            {
                "kind": "purge",
                "service": str(msg["from"]),
                "node": msg["pubsub_event"]["purge"]["node"],
            }
        )

    def _on_pubsub_delete(self, msg: Any) -> None:
        self._record_event(
            {
                "kind": "delete",
                "service": str(msg["from"]),
                "node": msg["pubsub_event"]["delete"]["node"],
            }
        )

    def _on_pubsub_config(self, msg: Any) -> None:
        self._record_event(
            {
                "kind": "config",
                "service": str(msg["from"]),
                "node": msg["pubsub_event"]["configuration"]["node"],
            }
        )

    def _on_pubsub_subscription(self, msg: Any) -> None:
        sub = msg["pubsub_event"]["subscription"]
        self._record_event(
            {
                "kind": "subscription",
                "service": str(msg["from"]),
                "node": sub["node"],
                "subscription": sub["subscription"],
                "subid": sub["subid"] or None,
            }
        )

    def drain_pubsub_events(
        self,
        node: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Pop matching pubsub events from the buffer (newest last)."""
        keep: deque[dict[str, Any]] = deque(maxlen=self._pubsub_events.maxlen)
        taken: list[dict[str, Any]] = []
        for ev in self._pubsub_events:
            if (
                (node is None or ev.get("node") == node)
                and (kind is None or ev.get("kind") == kind)
                and len(taken) < limit
            ):
                taken.append(ev)
            else:
                keep.append(ev)
        self._pubsub_events = keep
        return taken

    # --- pubsub ---------------------------------------------------------------

    @property
    def pubsub(self) -> Any:
        """Lazily build a :class:`PubSubClient` over the live xep_0060 plugin."""
        client = getattr(self, "_pubsub", None)
        if client is None:
            # Import here to avoid a hard dependency cycle at module import time.
            from .pubsub import PubSubClient

            client = PubSubClient(self.xmpp)
            self._pubsub = client
        return client

    @property
    def mam(self) -> Any:
        """Lazily build a :class:`MAMClient` over the live xep_0313 plugin."""
        client = getattr(self, "_mam", None)
        if client is None:
            from .mam import MAMClient

            client = MAMClient(self.xmpp)
            self._mam = client
        return client

    # --- service discovery ----------------------------------------------------

    async def disco_info(self, jid: str | None = None) -> dict[str, Any]:
        """Fetch disco#info (features + identities) for ``jid`` or the server."""
        target = jid or self.xmpp.boundjid.host
        try:
            iq = await self.xmpp.plugin["xep_0030"].get_info(jid=target, cached=False)
        except (IqError, IqTimeout) as exc:
            raise XMPPError(f"disco#info failed for {target}: {exc}") from exc
        info = iq["disco_info"]
        return {
            "jid": target,
            "features": sorted(info["features"]),
            "identities": [
                {"category": c, "type": t, "name": n}
                for (c, t, _lang, n) in info["identities"]
            ],
        }

    async def disco_items(self, jid: str | None = None) -> list[dict[str, str]]:
        """Fetch disco#items (child services) for ``jid`` or the server."""
        target = jid or self.xmpp.boundjid.host
        try:
            iq = await self.xmpp.plugin["xep_0030"].get_items(jid=target)
        except (IqError, IqTimeout) as exc:
            raise XMPPError(f"disco#items failed for {target}: {exc}") from exc
        return [
            {"jid": item[0], "node": item[1] or "", "name": item[2] or ""}
            for item in iq["disco_items"]["items"]
        ]
