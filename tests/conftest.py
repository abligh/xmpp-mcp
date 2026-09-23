"""Shared unit-test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_claude_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests hermetic when they run *inside* a Claude Code session.

    Settings discover the session that launched the process — which, under
    Claude Code, is the developer's own session: its name and ID would leak
    into every Settings() a test builds. Tests that want a session say so
    explicitly (``xmpp_claude_session=<path>``).
    """
    monkeypatch.setenv("XMPP_CLAUDE_SESSION", "off")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
