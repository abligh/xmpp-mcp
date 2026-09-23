"""The provider interface, plus the payload-poking helpers providers share."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def get(payload: Any, *keys: str) -> Any:
    """``payload[k1][k2]…`` or ``None`` if any step is missing or not a dict."""
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def obj(payload: Any, *keys: str) -> dict[str, Any]:
    """:func:`get` constrained to a dict.

    A webhook body is attacker-shaped: any field may hold any type, so a
    summariser that assumes ``payload["pull_request"]`` is an object turns a
    malformed delivery into a 500. Everything that gets indexed goes through
    here.
    """
    value = get(payload, *keys)
    return value if isinstance(value, dict) else {}


def count(value: Any) -> int:
    """Length of a list/dict field, 0 for anything else."""
    return len(value) if isinstance(value, (list, dict)) else 0


class Provider:
    """How to recognise, authenticate and describe one kind of webhook sender."""

    #: Short identifier; also names the credential setting (see
    #: ``RelaySettings.credential_for``).
    name = "provider"

    def matches(self, headers: Mapping[str, Any]) -> bool:
        """True if this request claims to come from this provider.

        Header-based and therefore *unauthenticated* — it only picks the
        verification method. :meth:`verify` decides whether the claim is true.
        """
        raise NotImplementedError

    def verify(self, headers: Mapping[str, Any], body: bytes, credential: str) -> bool:
        """True if the request proves it holds ``credential``."""
        raise NotImplementedError

    def delivery_id(self, headers: Mapping[str, Any]) -> str | None:
        """The sender's unique ID for this delivery, if it supplies one.

        Used to drop replays and repeats; ``None`` means the relay makes one up.
        """
        return None

    def event(self, headers: Mapping[str, Any], payload: Any) -> str | None:
        """The sender's name for this kind of event, for route matching."""
        return None

    def summarise(self, payload: Any, headers: Mapping[str, Any], path: str) -> str:
        """One human-readable line describing the event."""
        raise NotImplementedError
