"""``WEBHOOK_*`` configuration for the relay."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_csv(value: str | None) -> list[str]:
    """Split a comma-separated setting, dropping blanks and duplicates."""
    if not value:
        return []
    return list(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))


class RelaySettings(BaseSettings):
    """``WEBHOOK_*`` environment settings (an optional ``.env`` is read too)."""

    model_config = SettingsConfigDict(
        env_prefix="WEBHOOK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- XMPP -------------------------------------------------------------
    xmpp_jid: str = Field(..., description="JID the relay logs in as, e.g. webhook@example.com")
    xmpp_password: str = Field(..., description="Password for the relay account")
    xmpp_host: str | None = Field(None, description="Server host, if not the JID domain")
    xmpp_port: int = Field(5222, description="C2S port")
    xmpp_tls_insecure: bool = Field(
        False, description="Skip TLS certificate checks (lab/self-signed only)"
    )
    xmpp_nick: str = Field("webhook", description="Nick used in rooms")

    # --- HTTP -------------------------------------------------------------
    http_host: str = Field("127.0.0.1", description="Interface to listen on")
    http_port: int = Field(8788, description="Port to listen on")
    max_request_bytes: int = Field(
        5 * 1024 * 1024, description="Largest accepted request body (bytes)"
    )

    # --- caller credentials ------------------------------------------------
    # One per provider, plus a shared token every provider accepts. If any of
    # these is set, every request must satisfy one of them (see auth.py).
    token: str | None = Field(
        None,
        description="Shared secret for any sender: Authorization: Bearer … or X-Webhook-Token",
    )
    github_secret: str | None = Field(
        None, description="GitHub webhook secret, verified as an X-Hub-Signature-256 HMAC"
    )
    gitlab_token: str | None = Field(
        None, description="GitLab webhook token, compared against X-Gitlab-Token"
    )

    # --- delivery ---------------------------------------------------------
    default_to: str | None = Field(None, description="Recipient JID when a request names none")
    default_room: str | None = Field(
        None, description="Room JID when a request names none (wins over default_to)"
    )
    routes: str | None = Field(
        None, description="Path to a TOML route table (see routes.py) for payload-based routing"
    )
    envelope: bool = Field(
        True,
        description='Honour an {"xmpp": {"to"|"room": …}} envelope in JSON payloads',
    )
    directory_room: str | None = Field(
        None,
        description=(
            "Room the relay joins to see which agents are online, so targets "
            "can be friendly names (e.g. to = \"Reviewer\") resolved at delivery"
        ),
    )
    allowed_targets: str | None = Field(
        None,
        description=(
            "Comma-separated fnmatch patterns limiting which JIDs may be "
            "addressed (e.g. '*@example.com,agents@conference.example.com'). "
            "Unset means any JID the server will route to"
        ),
    )
    message_type: Literal["chat", "normal", "headline"] = Field(
        "chat",
        description=(
            "Type for one-to-one messages. chat/normal are stored offline by the "
            "server if the agent is away (RFC 6121 §8.5.2); headline is not"
        ),
    )
    max_message_bytes: int = Field(
        48_000,
        ge=256,
        description=(
            "Cap on the serialised message, counted in bytes after XML escaping. "
            "Keep it under the server's stanza size limit (ejabberd's default is "
            "64 KiB); longer payloads are truncated"
        ),
    )
    json_container: bool = Field(
        True, description="Also attach the payload as a XEP-0335 JSON container when it fits"
    )
    queue_size: int = Field(
        1000, ge=1, description="Max webhooks waiting for XMPP delivery"
    )
    dedupe_size: int = Field(
        512,
        ge=0,
        description=(
            "Remember this many recent delivery IDs and drop repeats. Blocks "
            "replay of a captured signed delivery; 0 disables"
        ),
    )

    def credential_for(self, provider: str) -> str | None:
        """The configured secret for a provider name, if any."""
        return {
            "github": self.github_secret,
            "gitlab": self.gitlab_token,
            "generic": self.token,
        }.get(provider)

    @property
    def any_credential_configured(self) -> bool:
        """True if the relay has been given any way to authenticate callers."""
        return any((self.token, self.github_secret, self.gitlab_token))
