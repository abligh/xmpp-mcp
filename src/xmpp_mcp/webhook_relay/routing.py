"""Choosing the XMPP target(s) for an inbound webhook.

Generic — nothing here knows which system sent the request. Four sources are
consulted, and the **first that yields anything wins**:

1. **Explicit** — the caller names the target in the request:
   ``POST /agent/<jid>`` or ``/room/<room-jid>``, else ``?to=`` / ``?room=``,
   else the ``X-XMPP-To`` / ``X-XMPP-Room`` headers.
2. **Envelope** — the JSON payload carries ``{"xmpp": {"to": …}}`` or
   ``{"xmpp": {"room": …}}`` (for senders that control their own payload; off
   with ``WEBHOOK_ENVELOPE=false``). The ``xmpp`` key is stripped before the
   payload is forwarded.
3. **Route table** — every operator-written route that matches the payload
   (see :mod:`.routes`). This is the one for GitHub and friends.
4. **Defaults** — ``WEBHOOK_DEFAULT_ROOM``, else ``WEBHOOK_DEFAULT_TO``.

An agent target may be a **friendly name** instead of a JID (anything without
an ``@``): the relay resolves it at delivery time against the agents in
``WEBHOOK_DIRECTORY_ROOM``, so "send PRs to the Reviewer" keeps working as
sessions come and go. Rooms are always JIDs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

from slixmpp import JID
from slixmpp.jid import InvalidJID

from .routes import Route

ENVELOPE_KEY = "xmpp"


class RoutingError(ValueError):
    """The request does not name a valid target."""


@dataclass(frozen=True)
class Target:
    kind: Literal["agent", "room"]
    jid: str  # a JID — or, for kind "agent", possibly a friendly name

    @property
    def is_name(self) -> bool:
        """True for a friendly-name target, to be resolved at delivery time."""
        return self.kind == "agent" and "@" not in self.jid


def _valid(value: Any, kind: str) -> Target:
    if not isinstance(value, str) or not value.strip():
        raise RoutingError(f"invalid {kind} target {value!r}")
    text = value.strip()
    if kind == "agent" and "@" not in text:
        return Target("agent", text)  # a friendly name
    try:
        jid = JID(text)
    except InvalidJID as exc:
        raise RoutingError(f"invalid {kind} JID {value!r}: {exc}") from exc
    if not jid.domain or (kind == "room" and (not jid.user or jid.resource)):
        raise RoutingError(f"invalid {kind} JID {value!r}")
    return Target(kind, jid.full)  # type: ignore[arg-type]


def target_allowed(jid: str, patterns: list[str]) -> bool:
    """True if ``jid`` matches one of ``patterns`` (empty patterns allow all)."""
    if not patterns:
        return True
    lowered = jid.lower()
    return any(fnmatchcase(lowered, p.strip().lower()) for p in patterns if p.strip())


def _explicit(path: str, query: Mapping[str, str], headers: Mapping[str, str]) -> Target | None:
    kind, _, rest = path.strip("/").partition("/")
    if kind in ("agent", "room") and rest:
        # ``rest`` may itself contain "/" — a full JID's resource.
        return _valid(rest, kind)
    for to, room in (
        (query.get("to"), query.get("room")),
        (headers.get("X-XMPP-To"), headers.get("X-XMPP-Room")),
    ):
        if to and room:
            raise RoutingError("name either a recipient or a room, not both")
        if to:
            return _valid(to, "agent")
        if room:
            return _valid(room, "room")
    return None


def _envelope(payload: Any) -> Target | None:
    env = payload.get(ENVELOPE_KEY) if isinstance(payload, dict) else None
    if env is None:
        return None
    if not isinstance(env, dict):
        raise RoutingError(f'"{ENVELOPE_KEY}" must be an object with "to" or "room"')
    to, room = env.get("to"), env.get("room")
    if bool(to) == bool(room):
        raise RoutingError(f'"{ENVELOPE_KEY}" must give exactly one of "to" or "room"')
    return _valid(to, "agent") if to else _valid(room, "room")


def strip_envelope(payload: Any) -> Any:
    """The payload as forwarded: without the routing envelope."""
    if isinstance(payload, dict) and ENVELOPE_KEY in payload:
        return {k: v for k, v in payload.items() if k != ENVELOPE_KEY}
    return payload


@dataclass(frozen=True)
class Routing:
    targets: tuple[Target, ...]
    how: Literal["explicit", "envelope", "routes", "default"]
    routes: tuple[str, ...] = ()  # names of the routes that matched


def resolve_targets(
    *,
    path: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    payload: Any = None,
    provider: str = "generic",
    event: str | None = None,
    routes: Sequence[Route] = (),
    envelope: bool = True,
    default_to: str | None = None,
    default_room: str | None = None,
) -> Routing:
    """Work out where a webhook goes (see the module docstring for precedence)."""
    target = _explicit(path, query, headers)
    if target:
        return Routing((target,), "explicit")
    if envelope:
        target = _envelope(payload)
        if target:
            return Routing((target,), "envelope")
    matched = [r for r in routes if r.matches(provider, event, path, payload)]
    if matched:
        targets = list(dict.fromkeys(
            _valid(r.to, "agent") if r.to else _valid(r.room, "room") for r in matched
        ))
        return Routing(tuple(targets), "routes", tuple(r.name for r in matched))
    if default_room:
        return Routing((_valid(default_room, "room"),), "default")
    if default_to:
        return Routing((_valid(default_to, "agent"),), "default")
    raise RoutingError(
        "no target: POST to /agent/<jid> or /room/<jid>, pass ?to= / ?room=, "
        f'add an "{ENVELOPE_KEY}" envelope, configure WEBHOOK_ROUTES, or set '
        "WEBHOOK_DEFAULT_TO / WEBHOOK_DEFAULT_ROOM"
    )


def resolve_target(
    path: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    default_to: str | None = None,
    default_room: str | None = None,
) -> Target:
    """Single-target routing from the request and defaults only.

    Kept for callers that have no payload in hand; see :func:`resolve_targets`.
    """
    routing = resolve_targets(
        path=path, query=query, headers=headers, envelope=False,
        default_to=default_to, default_room=default_room,
    )
    return routing.targets[0]
