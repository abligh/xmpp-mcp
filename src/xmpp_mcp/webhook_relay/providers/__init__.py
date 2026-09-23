"""Webhook *providers*: the only place that knows about a specific sender.

Everything else in the relay is source-agnostic — it routes, formats, queues
and delivers whatever arrives. A provider adds the two things that cannot be
generic:

* **how to authenticate it.** Each system proves who it is differently:
  GitHub signs the body (HMAC-SHA256 in ``X-Hub-Signature-256``), GitLab
  echoes a shared token in ``X-Gitlab-Token``, others use a bearer token.
* **how to describe it.** Turning a payload into one useful line means
  knowing where that system puts the interesting fields.

A provider is selected per request by :func:`detect`, which looks only at
headers. The generic provider matches everything and is the fallback.

Adding one (say Grafana) takes three steps:

1. Write a :class:`Provider` subclass here in ``providers/``.
2. Add its credential to :class:`~xmpp_mcp.webhook_relay.settings.RelaySettings`
   and to ``credential_for``.
3. Register the class in :data:`PROVIDERS`.

Nothing in the core changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import Provider
from .generic import GenericProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

# Ordered: the first whose headers match wins. Generic is not in the list —
# it is the fallback, and it matches everything.
PROVIDERS: tuple[Provider, ...] = (GitHubProvider(), GitLabProvider())
GENERIC: Provider = GenericProvider()

__all__ = [
    "Provider", "GenericProvider", "GitHubProvider", "GitLabProvider",
    "PROVIDERS", "GENERIC", "detect", "provider_names",
]


def detect(headers: Mapping[str, Any]) -> Provider:
    """Return the provider whose signature headers are present, else generic."""
    for provider in PROVIDERS:
        if provider.matches(headers):
            return provider
    return GENERIC


def provider_names() -> list[str]:
    return [p.name for p in (*PROVIDERS, GENERIC)]
