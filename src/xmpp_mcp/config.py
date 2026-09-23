"""Environment-driven configuration for the XMPP MCP server."""

from __future__ import annotations

from typing import Self

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from slixmpp import JID
from slixmpp.jid import InvalidJID

from .claude_session import ClaudeSession, resolve as resolve_claude_session
from .identity import IdentityError, expand_template, short_hostname


class Settings(BaseSettings):
    """Settings loaded from environment variables (and an optional .env file).

    The XMPP_* group configures the client connection used for all messaging,
    MUC, presence and discovery tools — it works against any RFC 6120/6121
    server (Openfire, Isode M-Link, ejabberd, Prosody).

    The OPENFIRE_* group is optional and only enables the Openfire REST admin
    tools. When unset, those tools fail with a clear message.

    The agent / channel group turns one server instance into one addressable
    *agent* on a shared XMPP network: a per-session JID (``XMPP_JID`` may be a
    template, see :mod:`xmpp_mcp.identity`) and, with ``XMPP_CHANNEL``, push
    delivery of inbound messages into Claude Code via the channels API.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Lets tests and the CLI pass ``xmpp_agent_id=...`` even though that
        # field reads its environment value through an alias.
        populate_by_name=True,
    )

    # --- XMPP client connection ------------------------------------------------
    xmpp_jid: str = Field(
        ...,
        description=(
            "JID of the account, e.g. bot@example.com. May be a template using "
            "{agent}, {host} and {fqdn}, e.g. {agent}.{host}@example.com — "
            "expanded (and normalised) at load time"
        ),
    )
    xmpp_password: str | None = Field(
        None,
        description="Password for the account. Not needed with XMPP_HOST_KEY_FILE",
    )
    xmpp_host_key_file: str | None = Field(
        None,
        description=(
            "Host key file (xmpp-mcp-keys host-key). When set, a password is "
            "derived for this agent's JID on every connect instead of XMPP_PASSWORD "
            "— one secret per host, one identity per session"
        ),
    )
    xmpp_credential_ttl: int = Field(
        24 * 3600, ge=60, description="Lifetime of each derived password (seconds)"
    )
    xmpp_ca_file: str | None = Field(
        None,
        description="CA bundle to verify the server's certificate with (a private CA)",
    )
    xmpp_host: str | None = Field(
        None,
        description="Server host to connect to, if it differs from the JID domain",
    )
    xmpp_port: int = Field(5222, description="C2S port")
    xmpp_tls_insecure: bool = Field(
        False,
        description="Skip TLS certificate verification (lab/self-signed servers only)",
    )
    xmpp_nick: str = Field("xmpp-mcp", description="Default nickname used when joining MUC rooms")
    xmpp_connect_timeout: float = Field(
        30.0, description="Seconds to wait for the XMPP session to establish"
    )
    xmpp_inbox_size: int = Field(
        500,
        ge=1,
        # Also caps the channel's pending-push queue. Zero would mean
        # "unbounded" to both deque and asyncio.Queue — the opposite of what
        # anyone setting it to zero would expect.
        description="Max number of inbound messages buffered in memory",
    )
    xmpp_register: bool = Field(
        False,
        description=(
            "Create the account with XEP-0077 in-band registration if it does "
            "not exist yet. Lab use: needs a server that allows open registration"
        ),
    )
    xmpp_muc_service: str | None = Field(
        None,
        description=(
            "Room service for list_rooms, e.g. conference.example.com. Found by "
            "service discovery when unset"
        ),
    )
    xmpp_auto_join: str | None = Field(
        None,
        description=(
            "Comma-separated MUC room JIDs to join at startup (and re-join after "
            "a reconnect), e.g. a shared agents@conference.example.com directory room"
        ),
    )

    # --- Agent identity -------------------------------------------------------
    xmpp_agent_name: str | None = Field(
        None,
        description=(
            "Name of this agent as known in Claude Code. Fills {agent} in the "
            "JID template and is the default MUC nick"
        ),
    )
    xmpp_agent_id: str | None = Field(
        None,
        # Claude Code exports CLAUDE_CODE_SESSION_ID to the servers it spawns,
        # which is exactly the "internal ID" peers want to see.
        validation_alias=AliasChoices("XMPP_AGENT_ID", "CLAUDE_CODE_SESSION_ID"),
        description="Internal ID of this agent (defaults to CLAUDE_CODE_SESSION_ID)",
    )
    xmpp_display_name: str | None = Field(
        None,
        description=(
            "Human-facing name advertised to peers. Defaults to the Claude Code "
            "session's name (and follows it when it changes), else the agent name"
        ),
    )
    xmpp_claude_session: str = Field(
        "auto",
        description=(
            "Where to read the Claude Code session file from: 'auto' (find the "
            "session that launched this server), 'off', or a path to the file"
        ),
    )
    xmpp_claude_session_poll: float = Field(
        10.0, gt=0, description="Seconds between checks of the session file for a rename"
    )
    xmpp_agent_host: str | None = Field(
        None,
        description=(
            "Host name used for {host} and advertised to peers. Defaults to the "
            "short hostname — override it when container hostnames are random"
        ),
    )

    # --- Claude Code channel --------------------------------------------------
    xmpp_channel: bool = Field(
        False,
        description=(
            "Declare the claude/channel capability and push every inbound "
            "message to Claude Code as a notifications/claude/channel event"
        ),
    )
    xmpp_channel_allow: str | None = Field(
        None,
        description=(
            "Comma-separated sender patterns allowed through the channel "
            "(fnmatch-style: alice@example.com, *@example.com, "
            "room@conference.example.com/*, *). Defaults to *@<own domain>"
        ),
    )

    # --- Openfire REST API admin (optional) -----------------------------------
    openfire_base_url: str | None = Field(
        None,
        description="Base URL of the Openfire REST API plugin, e.g. http://openfire:9090",
    )
    openfire_secret_key: str | None = Field(
        None, description="Openfire REST API shared secret key (Authorization header)"
    )
    openfire_admin_user: str | None = Field(
        None, description="Openfire admin username (alternative to secret key)"
    )
    openfire_admin_password: str | None = Field(
        None, description="Openfire admin password (used with openfire_admin_user)"
    )
    openfire_verify_tls: bool = Field(
        True, description="Verify TLS certificates when calling the Openfire REST API"
    )

    # The Claude Code session this server runs under, if one was found.
    _claude_session: ClaudeSession | None = PrivateAttr(default=None)
    # Whether XMPP_NICK was given. Recorded before the validator defaults the
    # nick, because in pydantic v2 that assignment itself marks the field as
    # set — model_fields_set can no longer tell "given" from "defaulted".
    _nick_explicit: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _resolve_identity(self) -> Self:
        """Find the Claude session, expand the JID template, default the nick."""
        self._claude_session = resolve_claude_session(self.xmpp_claude_session)
        session = self._claude_session
        if session is not None and not self.xmpp_agent_id:
            self.xmpp_agent_id = session.session_id
        try:
            jid = expand_template(
                self.xmpp_jid,
                session=session.session_id if session else self.xmpp_agent_id,
                agent=self.xmpp_agent_name,
                host=self.xmpp_agent_host,
            )
            parsed = JID(jid)
        except (IdentityError, InvalidJID) as exc:
            raise ValueError(f"Invalid XMPP_JID {self.xmpp_jid!r}: {exc}") from exc
        if not parsed.user:
            raise ValueError(f"XMPP_JID {jid!r} has no localpart (expected user@domain)")
        self.xmpp_jid = jid
        if not self.xmpp_password and not self.xmpp_host_key_file:
            raise ValueError("set XMPP_PASSWORD, or XMPP_HOST_KEY_FILE for derived credentials")
        # An agent's natural room nick is its friendly name. Only applied when
        # the nick was left at its default — an explicit XMPP_NICK always wins.
        self._nick_explicit = "xmpp_nick" in self.model_fields_set
        if not self._nick_explicit and self.has_friendly_name:
            self.xmpp_nick = self.display_name
        return self

    @property
    def claude_session(self) -> ClaudeSession | None:
        return self._claude_session

    @property
    def nick_is_explicit(self) -> bool:
        """True if XMPP_NICK was configured (then room nicks never follow renames)."""
        return self._nick_explicit

    @property
    def has_friendly_name(self) -> bool:
        """True when some source supplies a friendly name (else the localpart is used)."""
        return bool(
            self.xmpp_display_name
            or (self._claude_session and self._claude_session.name)
            or self.xmpp_agent_name
        )

    @property
    def agent_host(self) -> str:
        """Host name advertised to peers."""
        return self.xmpp_agent_host or short_hostname()

    @property
    def display_name(self) -> str:
        """Friendly name at startup: explicit, else the Claude session's, else the agent name.

        The session's name can change later; ``XMPPClient`` follows it.
        """
        session_name = self._claude_session.name if self._claude_session else None
        return (
            self.xmpp_display_name
            or session_name
            or self.xmpp_agent_name
            or JID(self.xmpp_jid).user
        )

    @property
    def display_name_is_fixed(self) -> bool:
        """An explicit XMPP_DISPLAY_NAME pins the name; otherwise it follows the session."""
        return bool(self.xmpp_display_name)

    @property
    def auto_join_rooms(self) -> list[str]:
        """Rooms from ``XMPP_AUTO_JOIN`` as a de-duplicated list."""
        return _split_csv(self.xmpp_auto_join)

    @property
    def channel_allow_patterns(self) -> list[str]:
        """Sender allowlist for the channel; defaults to every account on our own domain."""
        patterns = _split_csv(self.xmpp_channel_allow)
        return patterns or [f"*@{JID(self.xmpp_jid).domain}"]

    @property
    def openfire_enabled(self) -> bool:
        """True when enough Openfire settings are present to attempt admin calls."""
        if not self.openfire_base_url:
            return False
        has_secret = bool(self.openfire_secret_key)
        has_basic = bool(self.openfire_admin_user and self.openfire_admin_password)
        return has_secret or has_basic


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated setting, dropping blanks and duplicates (order kept)."""
    if not value:
        return []
    return list(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))


def load_settings(**overrides: object) -> Settings:
    """Load settings from the environment. Raises if required XMPP_* vars are missing.

    ``overrides`` (field name → value, e.g. from command-line flags) take
    precedence over the environment and ``.env``. ``None`` values are ignored
    so an unset CLI flag never masks an environment variable.
    """
    given = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**given)  # type: ignore[arg-type]
