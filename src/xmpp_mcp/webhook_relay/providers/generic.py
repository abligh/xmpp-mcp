"""The fallback provider: any HTTP POST, authenticated by the shared token."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..auth import same_secret
from .base import Provider


class GenericProvider(Provider):
    """Matches anything, describes the request rather than the payload.

    Its credential is ``WEBHOOK_TOKEN``, the same bearer token every provider
    also accepts — so a curl, a CI script or a monitoring system needs no
    provider of its own.
    """

    name = "generic"

    def matches(self, headers: Mapping[str, Any]) -> bool:
        return True

    def verify(self, headers: Mapping[str, Any], body: bytes, credential: str) -> bool:
        given = bearer_token(headers)
        return bool(given) and same_secret(given, credential)

    def delivery_id(self, headers: Mapping[str, Any]) -> str | None:
        # An arbitrary sender may still offer one; honour it for de-duplication.
        return headers.get("X-Webhook-Delivery") or headers.get("X-Request-Id")

    def event(self, headers: Mapping[str, Any], payload: Any) -> str | None:
        # A free-form sender may say what it is sending; routes can match it.
        return headers.get("X-Webhook-Event")

    def summarise(self, payload: Any, headers: Mapping[str, Any], path: str) -> str:
        return f"Webhook POST {path}"


def bearer_token(headers: Mapping[str, Any]) -> str:
    """The caller's shared token, from ``X-Webhook-Token`` or ``Authorization``."""
    header = headers.get("X-Webhook-Token")
    if header:
        return str(header)
    auth = str(headers.get("Authorization", ""))
    return auth[7:] if auth.lower().startswith("bearer ") else ""
