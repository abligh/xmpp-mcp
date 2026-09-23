"""Tests for environment-driven settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xmpp_mcp.config import Settings

_BASE = {"xmpp_jid": "bot@example.com", "xmpp_password": "secret"}


def _settings(**overrides: object) -> Settings:
    # _env_file=None keeps tests isolated from any real .env on disk.
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[arg-type]


def test_required_fields_have_defaults() -> None:
    s = _settings()
    assert s.xmpp_jid == "bot@example.com"
    assert s.xmpp_port == 5222
    assert s.xmpp_tls_insecure is False
    assert s.xmpp_nick == "xmpp-mcp"


def test_missing_required_field_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, xmpp_jid="bot@example.com")  # type: ignore[call-arg]


def test_openfire_disabled_without_base_url() -> None:
    assert _settings(openfire_secret_key="k").openfire_enabled is False


def test_openfire_enabled_with_secret_key() -> None:
    s = _settings(openfire_base_url="http://of:9090", openfire_secret_key="k")
    assert s.openfire_enabled is True


def test_openfire_enabled_with_basic_auth() -> None:
    s = _settings(
        openfire_base_url="http://of:9090",
        openfire_admin_user="admin",
        openfire_admin_password="pw",
    )
    assert s.openfire_enabled is True


def test_openfire_disabled_with_partial_basic_auth() -> None:
    s = _settings(openfire_base_url="http://of:9090", openfire_admin_user="admin")
    assert s.openfire_enabled is False


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XMPP_JID", "env@example.com")
    monkeypatch.setenv("XMPP_PASSWORD", "envpw")
    monkeypatch.setenv("XMPP_PORT", "5269")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.xmpp_jid == "env@example.com"
    assert s.xmpp_port == 5269


# --- agent identity / channel settings --------------------------------------


def test_jid_template_is_expanded() -> None:
    s = _settings(
        xmpp_jid="{agent}.{host}@example.com",
        xmpp_agent_name="Reviewer",
        xmpp_agent_host="host1",
    )
    assert s.xmpp_jid == "reviewer.host1@example.com"


def test_jid_template_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XMPP_JID", "{agent}@{host}")
    monkeypatch.setenv("XMPP_PASSWORD", "pw")
    monkeypatch.setenv("XMPP_AGENT_NAME", "myagent")
    monkeypatch.setenv("XMPP_AGENT_HOST", "host1.foo")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.xmpp_jid == "myagent@host1.foo"


def test_jid_template_without_agent_name_fails() -> None:
    with pytest.raises(ValidationError, match="XMPP_AGENT_NAME"):
        _settings(xmpp_jid="{agent}@example.com")


@pytest.mark.parametrize("bad", ["example.com", "{agnet}@example.com", "a@b@c"])
def test_invalid_jid_fails(bad: str) -> None:
    with pytest.raises(ValidationError):
        _settings(xmpp_jid=bad, xmpp_agent_name="x")


def test_nick_defaults_to_agent_name() -> None:
    s = _settings(xmpp_agent_name="Reviewer")
    assert s.xmpp_nick == "Reviewer"


def test_explicit_nick_beats_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(xmpp_agent_name="Reviewer", xmpp_nick="rev").xmpp_nick == "rev"
    monkeypatch.setenv("XMPP_NICK", "from-env")
    assert _settings(xmpp_agent_name="Reviewer").xmpp_nick == "from-env"


def test_agent_id_falls_back_to_claude_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-123")
    assert _settings().xmpp_agent_id == "sess-123"
    monkeypatch.setenv("XMPP_AGENT_ID", "explicit")
    assert _settings().xmpp_agent_id == "explicit"


def test_display_name_precedence() -> None:
    assert _settings().display_name == "bot"  # localpart
    assert _settings(xmpp_agent_name="Rev").display_name == "Rev"
    assert _settings(xmpp_agent_name="Rev", xmpp_display_name="Code Reviewer").display_name == (
        "Code Reviewer"
    )


def test_auto_join_rooms_split() -> None:
    s = _settings(xmpp_auto_join="agents@conf.example.com, ops@conf.example.com")
    assert s.auto_join_rooms == ["agents@conf.example.com", "ops@conf.example.com"]
    assert _settings().auto_join_rooms == []


def test_nick_explicitness_survives_defaulting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaulting the nick must not make it look configured.

    pydantic v2 counts a validator's own assignment as "set", so
    model_fields_set can't answer this after validation — which once meant
    room nicks never followed a rename.
    """
    assert _settings(xmpp_agent_name="Rev").nick_is_explicit is False
    assert _settings(xmpp_agent_name="Rev", xmpp_nick="rev").nick_is_explicit is True
    monkeypatch.setenv("XMPP_NICK", "from-env")
    assert _settings(xmpp_agent_name="Rev").nick_is_explicit is True
