"""Global webhook-to-XMPP relay.

A small, long-running HTTP service that turns incoming webhooks (GitHub,
GitLab, CI, monitoring — anything that can POST) into XMPP messages
addressed to an agent or a room. It replaces "spawn a headless agent to
forward this JSON with SendMessage": no model, no tokens, one persistent
XMPP connection per host.

    WEBHOOK_XMPP_JID=webhook@xmpp.test WEBHOOK_XMPP_PASSWORD=webhookpw \\
        xmpp-webhook-relay              # or: python -m xmpp_mcp.webhook_relay

    curl -X POST localhost:8788/agent/reviewer.host1@xmpp.test \\
         -H 'Content-Type: application/json' -d '{"hello": "world"}'

Layout — the split is between what every webhook needs and what one kind of
sender needs:

``settings.py``   WEBHOOK_* configuration
``routing.py``    which JID(s) a request is aimed at: explicit, envelope, routes, defaults
``routes.py``     the operator's route table (payload-based routing, TOML)
``stanza.py``     making arbitrary payload text safe and small enough for a stanza
``auth.py``       whether a caller may inject a message at all
``relay.py``      the XMPP connection, the delivery queue and the HTTP endpoint
``providers/``    **the only source-specific code**: how to authenticate and
                  summarise GitHub, GitLab, or anything else (see
                  ``providers/__init__.py`` for how to add one)

Routing — the first of these that is present wins:

1. Path: ``POST /agent/<jid>`` (one-to-one) or ``POST /room/<room-jid>``.
2. Query string: ``?to=<jid>`` or ``?room=<room-jid>``.
3. Headers: ``X-XMPP-To`` or ``X-XMPP-Room``.
4. Defaults: ``WEBHOOK_DEFAULT_ROOM``, else ``WEBHOOK_DEFAULT_TO``.

The message body is a one-line summary followed by the JSON payload, so any
XMPP client or agent can read it; the payload is also attached verbatim as a
XEP-0335 JSON container when it fits.

Delivery is at-most-once, but honest: a stanza handed to a dying stream is
retried after the reconnect and, failing that, counted as ``failed`` rather
than ``sent``.
"""

from __future__ import annotations

from .providers import GENERIC, PROVIDERS, Provider, detect
from .relay import Outgoing, WebhookRelay, serve
from .routes import Route, RouteError, load_routes, parse_routes
from .routing import (
    Routing, RoutingError, Target, resolve_target, resolve_targets, strip_envelope,
    target_allowed,
)
from .settings import RelaySettings
from .stanza import format_body, scrub, truncate_to_bytes, xml_cost

__all__ = [
    "GENERIC", "PROVIDERS", "Provider", "detect",
    "Outgoing", "WebhookRelay", "serve",
    "Route", "RouteError", "load_routes", "parse_routes",
    "Routing", "RoutingError", "Target", "resolve_target", "resolve_targets",
    "strip_envelope", "target_allowed",
    "RelaySettings", "format_body", "scrub", "truncate_to_bytes", "xml_cost",
    "main",
]


import argparse
import asyncio
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point (``xmpp-webhook-relay``)."""
    parser = argparse.ArgumentParser(
        prog="xmpp-webhook-relay",
        description=(
            "Relay HTTP webhooks to XMPP agents and rooms. Configure via "
            "WEBHOOK_* env vars; senders are recognised by "
            + ", ".join(p.name for p in PROVIDERS) + " providers."
        ),
    )
    parser.add_argument("--host", help="HTTP listen address (WEBHOOK_HTTP_HOST)")
    parser.add_argument("--port", type=int, help="HTTP listen port (WEBHOOK_HTTP_PORT)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    overrides = {
        k: v for k, v in {"http_host": args.host, "http_port": args.port}.items()
        if v is not None
    }
    settings = RelaySettings(**overrides)  # type: ignore[arg-type]
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass
