"""Payload-based routing: an operator-written route table.

Routing on the URL (``/agent/<jid>``) makes the *caller* choose the target.
That is fine for a curl, but awkward for GitHub — one webhook URL per repo
per destination — and weak for signed senders: GitHub's HMAC covers the body,
not the URL, so a target taken from the URL can be changed by replaying the
request elsewhere. A route table fixes both. The operator decides where each
kind of event goes, matching on fields of the (authenticated) payload::

    # routes.toml — point WEBHOOK_ROUTES at this file

    [[route]]
    name = "prs-to-reviewer"
    provider = "github"
    event = "pull_request"
    match = { "repository.full_name" = "abligh/xmpp-mcp", action = "opened" }
    to = "Reviewer"              # a friendly name, resolved via the directory room

    [[route]]
    provider = "github"
    event = "workflow_run"
    match = { "workflow_run.conclusion" = ["failure", "timed_out"] }
    room = "agents@conference.xmpp.test"

Each ``[[route]]`` takes:

``to`` / ``room``  exactly one: the target (a JID, or for ``to`` a friendly name)
``provider``       optional: ``github``, ``gitlab`` or ``generic``
``event``          optional glob on the provider's event name (``pull_request``,
                   ``Merge Request Hook``, …)
``path``           optional glob on the request path
``match``          optional table: dotted payload path → expected value. A
                   string is a glob, a list means "any of", anything else must
                   be equal. A missing path never matches.
``name``           optional label for logs

**Every** matching route delivers (duplicates collapse), so one event can go
to an agent and a room. Because the operator writes the targets, a payload
can only ever reach JIDs listed here — unlike the URL or the envelope.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .providers import provider_names

_KNOWN_KEYS = {"name", "to", "room", "provider", "event", "path", "match"}


class RouteError(ValueError):
    """The route table is malformed."""


@dataclass(frozen=True)
class Route:
    to: str | None = None
    room: str | None = None
    provider: str | None = None
    event: str | None = None
    path: str | None = None
    match: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""

    def matches(self, provider: str, event: str | None, path: str, payload: Any) -> bool:
        if self.provider and self.provider != provider:
            return False
        if self.event and not (event and fnmatchcase(event, self.event)):
            return False
        if self.path and not fnmatchcase(path, self.path):
            return False
        return all(_field_matches(payload, key, want) for key, want in self.match.items())


_MISSING = object()


def _lookup(payload: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(payload, dict) or part not in payload:
            return _MISSING
        payload = payload[part]
    return payload


def _field_matches(payload: Any, dotted: str, want: Any) -> bool:
    got = _lookup(payload, dotted)
    if got is _MISSING:
        return False
    if isinstance(want, list):
        return any(_value_matches(got, w) for w in want)
    return _value_matches(got, want)


def _value_matches(got: Any, want: Any) -> bool:
    if isinstance(want, str):
        # Globs compare against the text of scalars only: a dict or list in
        # the payload never matches a string, however permissive the glob.
        if isinstance(got, (dict, list)) or got is None:
            return False
        text = str(got).lower() if isinstance(got, bool) else str(got)
        return fnmatchcase(text, want)
    return got == want and type(got) is type(want)


def parse_routes(data: Mapping[str, Any], source: str = "routes") -> list[Route]:
    """Validate a parsed route table (``{"route": [...]}``)."""
    unknown_top = set(data) - {"route"}
    if unknown_top:
        raise RouteError(f"{source}: unknown top-level keys {sorted(unknown_top)}")
    entries = data.get("route", [])
    if not isinstance(entries, list):
        raise RouteError(f"{source}: 'route' must be an array of tables ([[route]])")
    routes: list[Route] = []
    providers = set(provider_names())
    for i, entry in enumerate(entries, 1):
        where = f"{source}: route #{i}"
        if not isinstance(entry, dict):
            raise RouteError(f"{where} is not a table")
        unknown = set(entry) - _KNOWN_KEYS
        if unknown:
            raise RouteError(f"{where}: unknown keys {sorted(unknown)}")
        to, room = entry.get("to"), entry.get("room")
        if bool(to) == bool(room):
            raise RouteError(f"{where}: give exactly one of 'to' or 'room'")
        for key in ("to", "room", "provider", "event", "path", "name"):
            if key in entry and not isinstance(entry[key], str):
                raise RouteError(f"{where}: '{key}' must be a string")
        if entry.get("provider") and entry["provider"] not in providers:
            raise RouteError(
                f"{where}: unknown provider {entry['provider']!r} "
                f"(expected one of {sorted(providers)})"
            )
        match = entry.get("match", {})
        if not isinstance(match, dict):
            raise RouteError(f"{where}: 'match' must be a table")
        routes.append(Route(
            to=to, room=room, provider=entry.get("provider"), event=entry.get("event"),
            path=entry.get("path"), match=dict(match), name=entry.get("name") or f"#{i}",
        ))
    return routes


def load_routes(path: str | Path) -> list[Route]:
    """Read and validate a TOML route table. Errors name the file and route."""
    p = Path(path).expanduser()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RouteError(f"cannot read route table {p}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RouteError(f"{p}: invalid TOML: {exc}") from exc
    return parse_routes(data, str(p))
