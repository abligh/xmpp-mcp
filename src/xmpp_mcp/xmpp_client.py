"""slixmpp-based XMPP client wrapper used by all messaging/MUC/presence tools.

Owns a single ``ClientXMPP`` connection for the life of the MCP server. The
connection is opened in the FastMCP lifespan and shared with every tool via the
lifespan context. Inbound messages are always buffered in a bounded deque so
tools can pull them (``get_recent_messages`` / ``search_messages``) — the
MCP model is request/response, so tools pull and the server does not push.

The connection is kept alive for the life of the server: an unexpected
disconnect schedules a reconnect with capped backoff, and every new session
re-announces presence and re-joins the rooms that were joined before.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections import deque
from datetime import datetime, timezone
from typing import Any

from slixmpp import JID, ClientXMPP, Presence
from slixmpp.exceptions import IqError, IqTimeout, PresenceError
from slixmpp.jid import InvalidJID
from slixmpp.xmlstream import register_stanza_plugin

from .agents import AGENT_NS, AgentInfo, PresenceCache, best_presence, is_available
from .claude_session import ClaudeSession, SessionWatcher
from .config import Settings
from .security_labels import SEC_LABEL_NS

logger = logging.getLogger("xmpp_mcp.xmpp")


class XMPPError(RuntimeError):
    """Raised when an XMPP operation fails in a way worth surfacing to the caller."""


# Backoff (seconds) before re-dialling after an established stream drops:
# quick first retry, then doubling to the cap. Note this governs *our*
# re-dial only — once connect() is running, retries of a refused connection
# are slixmpp's own loop, which grows to 300s (CLAUDE.md gotcha #14).
_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0

_DELAY_NS = "urn:xmpp:delay"


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


def _open_access_form(xmpp: Any) -> Any:
    """XEP-0060 §7.1.5 publish-options: make a PEP node readable by anyone."""
    form = xmpp.plugin["xep_0004"].make_form(ftype="submit")
    form.add_field(var="FORM_TYPE", ftype="hidden",
                   value="http://jabber.org/protocol/pubsub#publish-options")
    form.add_field(var="pubsub#access_model", value="open")
    return form


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
        # Every room this client has ever joined, including ones it has since
        # left or been dropped from. Lets tools tell "this JID is a room I am
        # not in" from "this JID is a person", so a reply can't be sent as a
        # 1:1 chat to a bare room JID (which the service just bounces).
        self._known_rooms: set[str] = set()
        # Pubsub notifications: bounded buffer that captures publish / retract /
        # purge / delete / config / subscription events on nodes we're subscribed
        # to. ``_seen_pubsub_msg_ids`` dedupes slixmpp's per-item event firing.
        self._pubsub_events: deque[dict[str, Any]] = deque(
            maxlen=settings.xmpp_inbox_size
        )
        self._seen_pubsub_msg_ids: deque[str] = deque(maxlen=200)
        # The last pubsub event stanza handled (see _on_pubsub_items).
        self._last_pubsub_xml: Any = None
        # Last available presence per full JID — powers list_agents.
        self.presence = PresenceCache()
        # Bare JIDs actually on the server-side roster. slixmpp's client_roster
        # also grows an entry for *any* JID that sends us presence (MUC rooms
        # included), so it can't answer "who are my contacts?" on its own.
        self._roster_jids: set[str] = set()
        self._stopping = False
        # Last presence we announced, replayed after a reconnect.
        self._presence: tuple[str | None, str | None] = (None, None)
        # The friendly name we advertise. Starts from Settings and, unless
        # XMPP_DISPLAY_NAME pins it, follows the Claude Code session's name.
        session = settings.claude_session
        self.friendly_name: str = settings.display_name
        self.name_source: str | None = (
            "user" if settings.display_name_is_fixed
            else session.name_source if session and session.name else None
        )
        self._session_watcher: SessionWatcher | None = None
        # Rooms whose nick tracks the friendly name (joined without an
        # explicit nick); renamed along with the agent.
        self._nick_follows: set[str] = set()
        self._reconnect_wait = _RECONNECT_MIN
        self._reconnect_task: asyncio.Task[None] | None = None

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

        # XEP-0172 User Nickname, published over PEP (XEP-0163): the standard
        # place for a self-chosen friendly name.
        self.xmpp.register_plugin("xep_0172")

        # Advertise agent metadata on every available presence we send
        # (initial, set_presence, MUC joins) and in disco#info.
        register_stanza_plugin(Presence, AgentInfo)
        self.xmpp.add_filter("out", self._stamp_agent_presence)
        self.xmpp.plugin["xep_0030"].add_feature(AGENT_NS)

        if settings.xmpp_register:
            # XEP-0077 in-band registration, run before SASL. If the account
            # already exists the server answers <conflict/> and we simply log
            # in as usual — so the flag is safe to leave on.
            self.xmpp.register_plugin("xep_0077", {"force_registration": True})
            self.xmpp.add_event_handler("register", self._on_register)

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
        self.xmpp.add_event_handler("disconnected", self._on_disconnected)
        # Contacts' presence arrives as ``presence``. MUC occupant presence
        # needs ``groupchat_presence`` as well: after an occupant's first
        # presence xep_0045 sets ``ignore_updates`` on it, which stops slixmpp
        # raising the generic event for that occupant. (Updates are
        # idempotent, so seeing the first one twice is harmless.)
        self.xmpp.add_event_handler("presence", self.presence.update)
        self.xmpp.add_event_handler("groupchat_presence", self.presence.update)
        self.xmpp.add_event_handler("groupchat_presence", self._on_muc_self_presence)
        self.xmpp.add_event_handler("roster_update", self._on_roster_update)
        # ``message`` fires for *every* incoming message stanza — including
        # groupchat. ``groupchat_message`` is an additional, narrower event
        # raised by xep_0045 for the same stanza, so subscribing to both
        # doubles every MUC line in the inbox. Subscribe once.
        self.xmpp.add_event_handler("message", self._on_message)
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

    def _connect(self) -> None:
        s = self._settings
        if s.xmpp_host:
            self.xmpp.connect(host=s.xmpp_host, port=s.xmpp_port)
        else:
            self.xmpp.connect()

    async def start(self) -> None:
        """Connect, block until the XMPP session is established, then auto-join rooms."""
        s = self._settings
        self._connect()
        try:
            await asyncio.wait_for(self._ready, timeout=s.xmpp_connect_timeout)
        except asyncio.TimeoutError as exc:
            raise XMPPError(
                f"Timed out after {s.xmpp_connect_timeout}s establishing the XMPP session"
            ) from exc
        logger.info("XMPP session established as %s", self.xmpp.boundjid.full)
        session = s.claude_session
        if session is not None:
            logger.info(
                "Claude Code session %s (found via %s); friendly name %r (%s)",
                session.session_id, session.how, self.friendly_name,
                self.name_source or "unknown source",
            )
            if not s.display_name_is_fixed:
                self._session_watcher = SessionWatcher(
                    session, self._on_claude_session_change,
                    interval=s.xmpp_claude_session_poll,
                )
                self._session_watcher.start()
        for room in s.auto_join_rooms:
            try:
                await self.join_room(room)
                logger.info("Auto-joined %s as %s", room, self._joined_rooms[room])
            except XMPPError as exc:
                # One bad room must not take the whole agent down.
                logger.warning("Auto-join of %s failed: %s", room, exc)

    async def stop(self) -> None:
        """Leave joined rooms and disconnect cleanly."""
        self._stopping = True
        if self._session_watcher is not None:
            await self._session_watcher.stop()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        for room, nick in list(self._joined_rooms.items()):
            try:
                self.xmpp.plugin["xep_0045"].leave_muc(room, nick)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Failed to leave room %s during shutdown", room, exc_info=True)
        self._joined_rooms.clear()
        # disconnect() only cancels an in-flight connection attempt when a
        # transport exists. Without this, a stop() issued while slixmpp is
        # retrying leaves its connect loop running: it can succeed later and
        # resurrect a "stopped" agent that peers still see as online.
        self.xmpp.cancel_connection_attempt()
        try:
            # Bounded: disconnect() waits for the server to close the stream,
            # which never happens if the connection was never established.
            await asyncio.wait_for(self.xmpp.disconnect(), timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.debug("Error during XMPP disconnect", exc_info=True)
        logger.info("XMPP connection closed")

    # --- event handlers -------------------------------------------------------

    async def _on_session_start(self, _event: Any) -> None:
        self._reconnect_wait = _RECONNECT_MIN
        try:
            # RFC 6121 §2.2: fetch the roster *before* sending initial
            # presence, so the server knows to send us contacts' presence.
            await self.xmpp.get_roster()
            show, status = self._presence
            self.xmpp.send_presence(pshow=show, pstatus=status)
        except (IqError, IqTimeout) as exc:
            if not self._ready.done():
                self._ready.set_exception(XMPPError(f"Roster fetch failed on login: {exc}"))
            return
        asyncio.ensure_future(self._publish_nick())
        if not self._ready.done():
            self._ready.set_result(True)
            return
        # A reconnect: occupancy does not survive the stream (XEP-0045 §7.2),
        # so rejoin every room we were in, under the same nick. One room
        # failing (nick taken by a ghost session, room now members-only) must
        # not stop the others.
        for room, nick in list(self._joined_rooms.items()):
            try:
                # A room that tracks our name re-joins under the *current*
                # name, which may have changed while we were away.
                await self.join_room(room, None if room in self._nick_follows else nick)
                logger.info("Re-joined %s after reconnect", room)
            except XMPPError as exc:
                logger.warning("Re-join of %s after reconnect failed: %s", room, exc)
        if any(self._presence):
            show, status = self._presence
            self.set_presence(show=show, status=status)

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
            if 303 in codes:
                # A room may re-broadcast the presence we *joined* with under
                # the new nick (ejabberd does), so occupants would still see
                # our old <agent name>. A status update under the new nick
                # replaces it.
                show, status = self._presence
                self.xmpp.send_presence(pto=f"{room}/{nick}", pshow=show, pstatus=status)

    def _on_roster_update(self, iq: Any) -> None:
        """Track server roster membership from roster results and pushes (RFC 6121 §2.1)."""
        for jid, item in iq["roster"]["items"].items():
            if item["subscription"] == "remove":
                self._roster_jids.discard(JID(jid).bare)
            else:
                self._roster_jids.add(JID(jid).bare)

    async def _on_register(self, _form: Any) -> None:
        """XEP-0077 §3.1: register ``localpart`` / password before authenticating."""
        iq = self.xmpp.Iq()
        iq["type"] = "set"
        iq["register"]["username"] = self.xmpp.boundjid.user
        iq["register"]["password"] = self._settings.xmpp_password
        try:
            await iq.send()
            logger.info("Registered new XMPP account %s", self.xmpp.boundjid.bare)
        except IqError as exc:
            cond = exc.iq["error"]["condition"]
            if cond == "conflict":
                logger.debug("Account %s already exists", self.xmpp.boundjid.bare)
            else:
                logger.warning("In-band registration refused (%s); trying to log in", cond)
        except IqTimeout:
            logger.warning("In-band registration timed out; trying to log in")

    def _on_disconnected(self, reason: Any) -> None:
        # Presence is per-stream: nothing cached is true any more.
        self.presence.clear()
        # A start() timeout *cancels* _ready, and Future.exception() would
        # then raise CancelledError straight out of this handler — which is a
        # BaseException, so slixmpp's event loop would not contain it.
        if self._stopping or not self._ready.done():
            return  # shutting down, or still logging in
        if self._ready.cancelled() or self._ready.exception():
            return  # the initial login failed — start() reports it
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._reconnect(reason))

    async def _reconnect(self, reason: Any) -> None:
        wait = self._reconnect_wait
        self._reconnect_wait = min(_RECONNECT_MAX, wait * 2)
        logger.warning("XMPP connection lost (%s); reconnecting in %.0fs", reason, wait)
        await asyncio.sleep(wait)
        if not self._stopping:
            self._connect()

    def _stamp_agent_presence(self, stanza: Any) -> Any:
        """Outgoing filter: attach ``<agent/>`` metadata to available presence."""
        if isinstance(stanza, Presence) and is_available(stanza):
            if stanza.xml.find(f"{{{AGENT_NS}}}agent") is None:
                s = self._settings
                info = stanza["mcp_agent"]
                if s.xmpp_agent_id:
                    info["id"] = s.xmpp_agent_id
                info["name"] = self.friendly_name
                if self.name_source:
                    info["name-source"] = self.name_source
                info["host"] = s.agent_host
        return stanza

    # --- friendly name --------------------------------------------------------

    async def _publish_nick(self) -> None:
        """Publish the friendly name as a XEP-0172 nickname over PEP.

        Best effort: PEP is optional on a server, and peers find the name in
        our presence anyway. The node is made world-readable where the server
        supports publish-options, since agents that only share a room have no
        presence subscription to satisfy PEP's default access model.
        """
        nick = self.xmpp.plugin["xep_0172"]
        try:
            await nick.publish_nick(self.friendly_name, options=_open_access_form(self.xmpp))
        except (IqError, IqTimeout):
            try:
                await nick.publish_nick(self.friendly_name)
            except (IqError, IqTimeout) as exc:
                logger.debug("Could not publish XEP-0172 nickname: %s", exc)

    def _on_claude_session_change(self, old: ClaudeSession, new: ClaudeSession) -> Any:
        if new.name and new.name != self.friendly_name:
            return self.rename(new.name, new.name_source)
        if new.name_source != self.name_source and new.name == self.friendly_name:
            self.name_source = new.name_source
        return None

    async def rename(self, name: str, source: str | None = None) -> None:
        """Adopt a new friendly name and tell everyone who can see us.

        Roster contacts get a fresh broadcast presence; each room gets
        directed presence — to ``room/<new name>`` where our nick tracks the
        name (a nick change, XEP-0045 §7.6), to our current occupant JID
        elsewhere. The confirmed nick arrives as status 303 and is picked up
        by ``_on_muc_self_presence``; if the room refuses (the name is taken),
        we simply keep the old nick there.
        """
        old = self.friendly_name
        self.friendly_name, self.name_source = name, source
        logger.info("Friendly name changed: %r -> %r (%s)", old, name, source or "?")
        if not self.xmpp.is_connected():
            return  # the next session announces the new name anyway
        show, status = self._presence
        self.xmpp.send_presence(pshow=show, pstatus=status)
        for room, nick in list(self._joined_rooms.items()):
            target = name if room in self._nick_follows else nick
            self.xmpp.send_presence(pto=f"{room}/{target}", pshow=show, pstatus=status)
        await self._publish_nick()

    def friendly_name_of(self, jid: str) -> str | None:
        """A peer's friendly name, if it advertises one (or the roster names it)."""
        bare = JID(jid).bare
        name = self.presence.agent_name_of(bare)
        if name:
            return name
        if bare in self._roster_jids:
            return self.xmpp.client_roster[bare]["name"] or None
        return None

    def resolve_address(self, to: str) -> str:
        """Turn what a caller wrote into an address.

        Anything containing ``@`` is a JID and is used as given — canonical
        addressing is by JID, so the XMPP server does all the routing.
        Anything else is a *name*, looked up among the peers ``list_agents``
        can see: a friendly name, an internal agent (session) ID, a roster
        name or a room nick, case-insensitively. Exactly one match is
        required; an ambiguous name is an error listing the candidates,
        because friendly names are neither unique nor stable.
        """
        text = to.strip()
        if "@" in text:
            return text
        key = text.casefold()
        matches = []
        for entry in self.list_agents():
            names = {entry.get("name"), entry.get("agent_id"), entry.get("roster_name")}
            names |= {r["nick"] for r in entry["rooms"]}
            if key in {n.casefold() for n in names if n}:
                matches.append(entry)
        if len(matches) == 1:
            return matches[0]["address"]
        if not matches:
            raise XMPPError(
                f"No agent or contact named {text!r} — use a JID, or see list_agents"
            )
        choices = ", ".join(f"{m['name']} <{m['address']}>" for m in matches)
        raise XMPPError(f"{text!r} is ambiguous: {choices} — use the JID")

    def _on_failed_auth(self, _event: Any) -> None:
        if not self._ready.done():
            self._ready.set_exception(
                XMPPError("Authentication failed — check XMPP_JID and XMPP_PASSWORD")
            )

    def _on_connection_failed(self, reason: Any) -> None:
        if not self._ready.done():
            self._ready.set_exception(XMPPError(f"Connection failed: {reason}"))

    def _real_jid(self, occupant: JID) -> str | None:
        """Real bare JID of a MUC occupant, when the room discloses it."""
        muc = self.xmpp.plugin["xep_0045"]
        try:
            real = muc.get_jid_property(occupant.bare, occupant.resource, "jid")
        except Exception:  # noqa: BLE001 - unknown room/nick
            real = None
        if real:
            return JID(real).bare
        cached = self.presence.get(occupant.full)
        return cached["real_jid"] if cached else None

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

    # --- messaging ------------------------------------------------------------

    def send_chat(
        self, to: str, body: str, label: Any = None, thread: str | None = None
    ) -> None:
        """Send a 1:1 chat message. ``label`` is an optional raw XEP-0258 element."""
        parse_jid(to, "recipient JID")
        msg = self.xmpp.make_message(mto=to, mbody=body, mtype="chat")
        if thread:
            msg["thread"] = thread
        if label is not None:
            msg.appendxml(label)
        msg.send()

    def send_groupchat(
        self, room: str, body: str, label: Any = None, thread: str | None = None
    ) -> None:
        """Send a message to a MUC room. ``label`` is an optional raw XEP-0258 element."""
        room = room_key(room)
        if room not in self._joined_rooms:
            raise XMPPError(f"Not joined to room {room} — call join_room first")
        msg = self.xmpp.make_message(mto=room, mbody=body, mtype="groupchat")
        if thread:
            msg["thread"] = thread
        if label is not None:
            msg.appendxml(label)
        msg.send()

    def is_joined(self, room: str) -> bool:
        return room_key(room) in self._joined_rooms

    def is_known_room(self, jid: str) -> bool:
        """True if ``jid`` is a room this client has joined at some point."""
        return room_key(jid) in self._known_rooms

    def nick_in(self, room: str) -> str | None:
        """Our nick in ``room``, or ``None`` if not joined."""
        return self._joined_rooms.get(room_key(room))

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
        self._presence = (show, status)
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
        room = room_key(room)
        s = self._settings
        # Without an explicit nick (argument or XMPP_NICK) the room nick is the
        # friendly name, and follows it through renames.
        follows = nick is None and not s.nick_is_explicit
        if nick is None:
            nick = self.friendly_name if follows else s.xmpp_nick
        # Friendly names are not unique, so a default nick may already be
        # taken; fall back to a disambiguated one rather than failing.
        candidates = [nick]
        if follows:
            candidates += [f"{nick} ({s.agent_host})",
                           f"{nick} ({(s.xmpp_agent_id or JID(s.xmpp_jid).user)[:8]})"]
        muc = self.xmpp.plugin["xep_0045"]
        for attempt, candidate in enumerate(candidates, 1):
            try:
                await muc.join_muc_wait(
                    room, candidate, maxstanzas=0, timeout=s.xmpp_connect_timeout,
                )
                nick = candidate
                break
            except PresenceError as exc:
                if exc.condition == "conflict" and attempt < len(candidates):
                    logger.info("Nick %r is taken in %s; trying another", candidate, room)
                    continue
                # Nick conflict with no fallback left, members-only, banned,
                # password required… (XEP-0045 §7.2.x).
                raise XMPPError(f"Failed to join room {room}: {exc}") from exc
            except (IqError, IqTimeout, asyncio.TimeoutError) as exc:
                raise XMPPError(f"Failed to join room {room}: {exc}") from exc
        if follows:
            self._nick_follows.add(room)
        else:
            self._nick_follows.discard(room)
        # The room decides the nick, and it need not be the one we asked for
        # (§7.2.9 lets the service assign one, and prep can fold ours). Trust
        # the nick in the self-presence: the self-echo filter compares against
        # it, so a stale value would push our own messages back at us.
        self._joined_rooms[room] = muc.our_nicks.get(None, {}).get(room, nick)
        self._known_rooms.add(room)
        return {
            "room": room,
            "nick": self._joined_rooms[room],
            "occupants": self.room_occupants(room),
        }

    def leave_room(self, room: str) -> None:
        """Leave a previously joined MUC room."""
        room = room_key(room)
        nick = self._joined_rooms.pop(room, None)
        self._nick_follows.discard(room)
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

    @property
    def joined_rooms(self) -> list[str]:
        return list(self._joined_rooms)

    # --- agent directory ------------------------------------------------------

    def list_agents(
        self, include_offline: bool = True, agents_only: bool = False
    ) -> list[dict[str, Any]]:
        """Everyone this client can see: roster contacts plus occupants of joined rooms.

        One entry per peer, merged by real bare JID when it is known (a
        contact who is also in two rooms appears once, with both rooms
        listed). Occupants of anonymous rooms, whose real JID is hidden, get
        their own entry keyed by occupant JID. This client itself is omitted.

        Each entry: ``jid`` (canonical bare JID, or ``None`` if hidden),
        ``address`` (what to pass to ``reply`` / ``send_message``),
        ``agent_id`` / ``name`` / ``host`` (from the peer's ``<agent/>``
        presence extension, when it runs xmpp-mcp), ``is_agent``,
        ``presence`` (``available``/``chat``/``away``/``xa``/``dnd``/
        ``unavailable``), ``status`` and ``rooms`` (``[{room, nick}]``).
        """

        me = self.xmpp.boundjid.bare
        entries: dict[str, dict[str, Any]] = {}

        def entry_for(key: str, jid: str | None, fallback_name: str) -> dict[str, Any]:
            if key not in entries:
                entries[key] = {
                    "jid": jid,
                    "address": jid or key,
                    "agent_id": None,
                    "name": fallback_name,
                    "name_source": None,
                    "host": None,
                    "is_agent": False,
                    "presence": "unavailable",
                    "status": "",
                    "rooms": [],
                    "_seen": [],  # presence entries, resolved below
                }
            return entries[key]

        roster = self.xmpp.client_roster
        for jid in sorted(self._roster_jids):
            if jid == me or jid in self._joined_rooms:
                continue
            item = roster[jid]
            e = entry_for(jid, jid, item["name"] or JID(jid).user)
            if item["name"]:
                e["roster_name"] = item["name"]
            e["_seen"].extend(self.presence.resources(jid).values())

        muc = self.xmpp.plugin["xep_0045"]
        for room, my_nick in self._joined_rooms.items():
            for nick in muc.get_roster(room) or []:
                if nick == my_nick:
                    continue
                occupant = f"{room}/{nick}"
                real = self._real_jid(JID(occupant))
                if real == me:
                    continue
                e = entry_for(real or occupant, real, nick)
                e["rooms"].append({"room": room, "nick": nick})
                seen = self.presence.get(occupant)
                if seen:
                    e["_seen"].append(seen)

        result: list[dict[str, Any]] = []
        for e in entries.values():
            seen = e.pop("_seen")
            best = best_presence(seen)
            if best is not None:
                e["presence"] = best["show"]
                e["status"] = best["status"]
            agent = next((s["agent"] for s in seen if s.get("agent")), None)
            if agent:
                e["is_agent"] = True
                e["agent_id"] = agent.get("id") or None
                e["name"] = agent.get("name") or e["name"]
                e["name_source"] = agent.get("name-source") or None
                e["host"] = agent.get("host") or None
            if not include_offline and e["presence"] == "unavailable":
                continue
            if agents_only and not e["is_agent"]:
                continue
            result.append(e)
        result.sort(key=lambda e: (e["presence"] == "unavailable", e["address"]))
        return result

    # --- pubsub event capture -------------------------------------------------

    def _record_event(self, event: dict[str, Any]) -> None:
        event["timestamp"] = _now_iso()
        self._pubsub_events.append(event)

    def _on_pubsub_items(self, msg: Any) -> None:
        """Capture ``pubsub_publish`` / ``pubsub_retract`` notifications.

        slixmpp fires the event once per item, with the *full* msg attached on
        every fire — so we process all items on the first fire and skip the
        rest. Two traps here, both of which once hung the event loop:

        * **Never iterate the stanza itself.** A slixmpp stanza is its own
          iterator: ``for item in stanza`` resets an index stored *on the
          stanza*. xep_0060 is itself looping over these items when it fires
          this (synchronous) handler, so iterating them here rewinds its loop,
          and it fires us again — for ever. Take ``.iterables``, a plain list.
        * **Don't dedupe on the message id alone.** RFC 6120 makes ``id``
          optional and ejabberd omits it on PEP notifications, so the repeat
          fires are recognised by the stanza object instead.
        """
        if msg.xml is self._last_pubsub_xml:
            return  # a repeat fire for a stanza already processed
        self._last_pubsub_xml = msg.xml
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

        for item in list(msg["pubsub_event"]["items"].iterables):
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
