"""The relay core: one XMPP connection, a delivery queue and an HTTP endpoint.

Source-agnostic. Everything specific to GitHub, GitLab or any other sender
lives in :mod:`xmpp_mcp.webhook_relay.providers`; this module only asks the
selected provider three things — is this request authentic, what is its
delivery ID, and how should it be described.

The HTTP handler validates, formats and **queues**, answering ``200 OK``
immediately. A single worker drains the queue into XMPP once the session is
up (joining target rooms on demand — XEP-0045 §7.4 only lets occupants
post), reconnecting with capped backoff if the server goes away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import ssl
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from slixmpp import ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout, PresenceError

from ..agents import PresenceCache
from . import providers
from .auth import authorised
from .routes import load_routes
from .settings import RelaySettings, split_csv
from .stanza import format_body, scrub, xml_cost

logger = logging.getLogger("xmpp_mcp.webhook_relay")

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0


from .routing import RoutingError, Target, resolve_targets, strip_envelope, target_allowed


@dataclass
class Outgoing:
    """One formatted webhook, waiting for (or in the middle of) delivery."""

    target: Target
    body: str
    msg_id: str
    payload_json: str | None = None
    provider: str = "generic"
    attempts: int = field(default=0)


class WebhookRelay:
    """Persistent XMPP client + delivery queue + aiohttp app."""

    def __init__(self, settings: RelaySettings) -> None:
        self.settings = settings
        self.queue: asyncio.Queue[Outgoing] = asyncio.Queue(settings.queue_size)
        self.online = asyncio.Event()
        self.sent = 0
        self.failed = 0
        self._rooms: set[str] = set()
        self._stopping = False
        self._reconnect_wait = _RECONNECT_MIN
        self._worker: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self.allowed_targets = split_csv(settings.allowed_targets)
        # Loaded once, at startup: a malformed table should stop the relay
        # before it accepts traffic, not fail request by request.
        self.routes = load_routes(settings.routes) if settings.routes else []
        # Agents seen in the directory room, for friendly-name targets.
        self.directory = PresenceCache()
        # Recently seen delivery IDs, newest last. A GitHub signature covers
        # the body only — not the target — so without this a captured signed
        # delivery could be replayed at any JID, for ever.
        self._seen_ids: deque[str] = deque(maxlen=settings.dedupe_size or 1)
        self.duplicates = 0

        self.xmpp = ClientXMPP(settings.xmpp_jid, settings.xmpp_password)
        for xep in ("xep_0030", "xep_0045", "xep_0199", "xep_0335"):
            self.xmpp.register_plugin(xep)
        if settings.xmpp_host:
            # Same STARTTLS-only pin as xmpp_client.XMPPClient (CLAUDE.md gotcha #1).
            self.xmpp.enable_direct_tls = False
        if settings.xmpp_tls_insecure:
            logger.warning("WEBHOOK_XMPP_TLS_INSECURE is set — TLS certificate checks disabled")
            self.xmpp.ssl_context.check_hostname = False
            self.xmpp.ssl_context.verify_mode = ssl.CERT_NONE
            mechs = self.xmpp["feature_mechanisms"]
            mechs.unencrypted_plain = True
            mechs.unencrypted_scram = True
        self.xmpp.add_event_handler("session_start", self._on_session_start)
        self.xmpp.add_event_handler("disconnected", self._on_disconnected)
        self.xmpp.add_event_handler("failed_all_auth", self._on_failed_auth)
        # A kick, ban or room destruction drops our occupancy without touching
        # the c2s stream; the cached membership would then make every later
        # groupchat bounce. Forget the room so the next delivery re-joins.
        self.xmpp.add_event_handler("message_error", self._on_stanza_error)
        self.xmpp.add_event_handler("presence_error", self._on_stanza_error)
        self.xmpp.add_event_handler("groupchat_presence", self.directory.update)

    # --- XMPP lifecycle -------------------------------------------------------

    def _connect(self) -> None:
        s = self.settings
        if s.xmpp_host:
            self.xmpp.connect(host=s.xmpp_host, port=s.xmpp_port)
        else:
            self.xmpp.connect()

    async def _on_session_start(self, _event: Any) -> None:
        self._reconnect_wait = _RECONNECT_MIN
        try:
            await self.xmpp.get_roster()
        except (IqError, IqTimeout):
            logger.warning("Roster fetch failed; continuing")
        self.xmpp.send_presence()
        self._rooms.clear()  # occupancy never survives a stream
        self.directory.clear()
        if self.settings.directory_room:
            try:
                await self._ensure_joined(self.settings.directory_room)
            except RuntimeError as exc:
                logger.warning("Could not join directory room: %s", exc)
        self.online.set()
        logger.info("XMPP session established as %s", self.xmpp.boundjid.full)

    def _on_failed_auth(self, _event: Any) -> None:
        logger.error("XMPP authentication failed — check WEBHOOK_XMPP_JID / WEBHOOK_XMPP_PASSWORD")

    def _on_stanza_error(self, stanza: Any) -> None:
        room = stanza["from"].bare
        if room in self._rooms:
            logger.warning("Error from %s (%s); will re-join before the next delivery",
                           room, stanza["error"]["condition"])
            self._rooms.discard(room)

    def _on_disconnected(self, reason: Any) -> None:
        self.online.clear()
        self._rooms.clear()
        # Single-flight: slixmpp can fire `disconnected` several times while a
        # server flaps, and one reconnect task per event would race
        # connect() against itself and multiply the backoff.
        if self._stopping or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = asyncio.ensure_future(self._reconnect(reason))

    async def _reconnect(self, reason: Any) -> None:
        wait = self._reconnect_wait
        self._reconnect_wait = min(_RECONNECT_MAX, wait * 2)
        logger.warning("XMPP connection lost (%s); reconnecting in %.0fs", reason, wait)
        await asyncio.sleep(wait)
        if not self._stopping:
            self._connect()

    async def start(self) -> None:
        """Start connecting and the delivery worker. Does not wait for XMPP."""
        self._connect()
        self._worker = asyncio.get_running_loop().create_task(
            self._run_worker(), name="webhook-relay-worker"
        )

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._worker, self._reconnect_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._worker = self._reconnect_task = None
        # Kill any connect attempt slixmpp is still retrying on its own: with
        # no transport, disconnect() would not cancel it, and a later success
        # would revive a stopped relay.
        self.xmpp.cancel_connection_attempt()
        try:
            # disconnect() waits for the stream to close, which never happens
            # if we were never connected.
            await asyncio.wait_for(self.xmpp.disconnect(), timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.debug("Error during XMPP disconnect", exc_info=True)

    # --- delivery -------------------------------------------------------------

    def enqueue(self, item: Outgoing) -> bool:
        """Queue ``item`` for delivery. ``False`` if the queue is full."""
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        return True

    async def _run_worker(self) -> None:
        while True:
            item = await self.queue.get()
            await self._deliver_with_retries(item)

    async def _deliver_with_retries(self, item: Outgoing, tries: int = 3) -> bool:
        """Deliver ``item``, re-trying across reconnects. True if it went out.

        slixmpp drops its send queue when a stream dies and ``msg.send()``
        raises nothing, so "no exception" does not mean "delivered": the
        stream has to still be up once the bytes have been written. Retries
        re-use the same message id, so a peer can spot the rare duplicate.
        """
        for attempt in range(1, tries + 1):
            item.attempts = attempt
            await self.online.wait()
            try:
                await self._deliver(item)
            except Exception as exc:  # noqa: BLE001 - one bad webhook must not stop the worker
                self.failed += 1
                logger.error("Delivery of %s to %s failed: %s", item.msg_id, item.target.jid, exc)
                return False
            if await self._flushed():
                self.sent += 1
                return True
            logger.warning(
                "Stream dropped while sending %s (attempt %d/%d); retrying after reconnect",
                item.msg_id, attempt, tries,
            )
        self.failed += 1
        logger.error("Giving up on %s to %s after %d attempts",
                     item.msg_id, item.target.jid, tries)
        return False

    async def _flushed(self, timeout: float = 10.0) -> bool:
        """True once the stanza is on the wire; False if the stream died first.

        slixmpp writes straight to the asyncio transport, so "sent" means the
        transport's write buffer has drained while the session was still up.
        No transport at all means the bytes went nowhere.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.online.is_set() and loop.time() < deadline:
            transport = self.xmpp.transport
            if transport is None:
                return False
            if not transport.get_write_buffer_size():
                return True
            await asyncio.sleep(0.02)
        return False

    async def _ensure_joined(self, room: str) -> None:
        if room in self._rooms:
            return
        try:
            await self.xmpp.plugin["xep_0045"].join_muc_wait(
                room, self.settings.xmpp_nick, maxstanzas=0, timeout=15
            )
        except (PresenceError, IqError, IqTimeout, asyncio.TimeoutError) as exc:
            raise RuntimeError(f"could not join {room}: {exc}") from exc
        self._rooms.add(room)

    def resolve_name(self, name: str) -> str:
        """The bare JID of the one agent in the directory room called ``name``.

        Matches the advertised friendly name, the internal agent (session) ID
        or the room nick, case-insensitively — the same rules as the agents'
        own ``resolve_address``. Ambiguity is an error, never a guess.
        """
        room = self.settings.directory_room
        if not room:
            raise RuntimeError(
                f"cannot resolve name {name!r}: set WEBHOOK_DIRECTORY_ROOM to a room "
                "the agents join"
            )
        key = name.casefold()
        found: dict[str, str] = {}
        for occupant, entry in self.directory.resources(room).items():
            agent = entry.get("agent") or {}
            real = entry.get("real_jid")
            names = {agent.get("name"), agent.get("id"), occupant}
            if real and key in {n.casefold() for n in names if n}:
                found[real] = agent.get("name") or occupant
        if len(found) == 1:
            return next(iter(found))
        if not found:
            raise RuntimeError(f"no agent named {name!r} in {room}")
        raise RuntimeError(f"{name!r} is ambiguous in {room}: {sorted(found)}")

    async def _deliver(self, item: Outgoing) -> None:
        if item.target.is_name:
            # Late binding: a friendly name means whichever session holds it
            # now. The allow-list is re-checked against the resolved JID.
            jid = self.resolve_name(item.target.jid)
            if not target_allowed(jid, self.allowed_targets):
                raise RuntimeError(f"{item.target.jid} resolved to {jid}, which is not allowed")
            item.target = Target("agent", jid)
        if item.target.kind == "room":
            await self._ensure_joined(item.target.jid)
            mtype = "groupchat"
        else:
            mtype = self.settings.message_type
        msg = self.xmpp.make_message(mto=item.target.jid, mbody=item.body, mtype=mtype)
        msg["id"] = item.msg_id
        if item.payload_json is not None:
            msg["json"]["value"] = item.payload_json
        msg.send()
        logger.info("Relayed %s %s → %s (%s)",
                    item.provider, item.msg_id, item.target.jid, mtype)

    # --- HTTP -----------------------------------------------------------------

    async def handle_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        # Which sender this claims to be decides how it is authenticated and
        # how it is summarised. The claim is header-based and therefore
        # untrusted until verify() agrees.
        provider = providers.detect(request.headers)
        if not authorised(self.settings, provider, request.headers, body):
            logger.warning("Rejected unauthenticated %s webhook on %s",
                           provider.name, request.path)
            return web.json_response({"error": "unauthorised"}, status=401)
        text = body.decode("utf-8", errors="replace")
        payload: Any = None
        if request.content_type == "application/json" or text.lstrip()[:1] in ("{", "["):
            try:
                payload = json.loads(text) if text.strip() else None
            except json.JSONDecodeError as exc:
                if request.content_type == "application/json":
                    return web.json_response({"error": f"invalid JSON: {exc}"}, status=400)

        s = self.settings
        try:
            routing = resolve_targets(
                path=request.path, query=request.query, headers=request.headers,
                payload=payload, provider=provider.name,
                event=provider.event(request.headers, payload),
                routes=self.routes, envelope=s.envelope,
                default_to=s.default_to, default_room=s.default_room,
            )
        except RoutingError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        refused = [t.jid for t in routing.targets
                   if not t.is_name and not target_allowed(t.jid, self.allowed_targets)]
        if refused:
            return web.json_response(
                {"error": f"target(s) {refused} not in WEBHOOK_ALLOWED_TARGETS"}, status=403,
            )

        msg_id = scrub(provider.delivery_id(request.headers) or "")[:128]
        if msg_id and self._seen_ids.maxlen and self.settings.dedupe_size:
            if msg_id in self._seen_ids:
                self.duplicates += 1
                logger.warning("Dropped duplicate delivery %s", msg_id)
                return web.json_response({"status": "duplicate", "id": msg_id})
            self._seen_ids.append(msg_id)
        msg_id = msg_id or uuid.uuid4().hex

        payload_json: str | None = None
        if payload is not None:
            payload = strip_envelope(payload)
            # Compact form: fewer bytes in the stanza, same information.
            text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            payload_json = text

        summary = provider.summarise(payload, request.headers, request.path)
        body = format_body(summary, text, s.max_message_bytes)
        # The container repeats the payload, so only attach it when the whole
        # stanza still fits the byte budget.
        container = None
        if s.json_container and payload_json:
            container = scrub(payload_json)
            if xml_cost(body) + xml_cost(container) > s.max_message_bytes:
                container = None
        if self.queue.maxsize - self.queue.qsize() < len(routing.targets):
            return web.json_response({"error": "queue full, retry later"}, status=503)
        for i, target in enumerate(routing.targets):
            # One stanza per target; ids stay unique but share the delivery ID.
            item_id = msg_id if len(routing.targets) == 1 else f"{msg_id}-{i + 1}"
            self.enqueue(Outgoing(target=target, body=body, msg_id=item_id,
                                  payload_json=container, provider=provider.name))
        return web.json_response({
            "status": "queued",
            "id": msg_id,
            "routed_by": routing.how,
            **({"routes": list(routing.routes)} if routing.routes else {}),
            "targets": [{"to": t.jid, "kind": t.kind} for t in routing.targets],
        })

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "xmpp": "online" if self.online.is_set() else "offline",
                # Bare JID only: the resource is random per session and of no
                # use to a caller.
                "jid": self.xmpp.boundjid.bare if self.online.is_set() else None,
                "queued": self.queue.qsize(),
                "sent": self.sent,
                "failed": self.failed,
                "duplicates": self.duplicates,
                "providers": providers.provider_names(),
                "authenticated": self.settings.any_credential_configured,
            }
        )

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=self.settings.max_request_bytes)
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_post("/", self.handle_webhook)
        app.router.add_post("/{kind:agent|room}/{jid:.+}", self.handle_webhook)
        return app


async def serve(settings: RelaySettings) -> None:
    """Run the relay until SIGINT/SIGTERM."""
    if settings.http_host not in ("127.0.0.1", "::1", "localhost") and not (
        settings.any_credential_configured
    ):
        logger.warning(
            "Listening on %s without WEBHOOK_TOKEN or WEBHOOK_GITHUB_SECRET — "
            "anyone who can reach this port can message your agents",
            settings.http_host,
        )
    relay = WebhookRelay(settings)
    await relay.start()
    runner = web.AppRunner(relay.make_app())
    await runner.setup()
    site = web.TCPSite(runner, settings.http_host, settings.http_port)
    await site.start()
    logger.info("Webhook relay listening on http://%s:%s", settings.http_host, settings.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl-C still raises KeyboardInterrupt
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await relay.stop()
