"""Deciding whether a request may inject a message into the XMPP network.

The endpoint is the boundary between "anything that can open a TCP
connection" and "text that lands in front of an agent", so the rule is
deliberately blunt:

* If **no** credential is configured anywhere, the relay is open. That is only
  reasonable on the default ``127.0.0.1`` bind, and :func:`serve` warns when
  it is bound wider.
* If **any** credential is configured, every request must satisfy one of
  them. In particular a caller cannot drop its ``X-GitHub-*`` headers to be
  treated as "generic" and slip past a configured GitHub secret: the
  fallback needs ``WEBHOOK_TOKEN``, and if that is unset there is nothing to
  fall back to.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from .providers.base import Provider
    from .settings import RelaySettings


def same_secret(given: str, expected: str) -> bool:
    """Constant-time comparison of two secrets that arrived as text.

    ``hmac.compare_digest`` raises TypeError on non-ASCII ``str`` operands,
    and aiohttp decodes headers as latin-1 — so comparing header text
    directly lets anyone turn an auth check into a 500 by sending a high
    byte. Compare bytes instead.
    """
    return hmac.compare_digest(
        given.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


def authorised(
    settings: RelaySettings,
    provider: Provider,
    headers: Mapping[str, Any],
    body: bytes,
) -> bool:
    """True if this request proves it holds a configured credential."""
    from .providers.generic import GenericProvider

    if not settings.any_credential_configured:
        return True  # open relay; the operator was warned at startup

    credential = settings.credential_for(provider.name)
    if credential and provider.verify(headers, body, credential):
        return True

    # The shared token is accepted for every provider, so an operator can use
    # one secret for a mixed fleet of senders.
    token = settings.token
    if token and not isinstance(provider, GenericProvider):
        if GenericProvider().verify(headers, body, token):
            return True
    return False
