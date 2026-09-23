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


def pytest_addoption(parser: pytest.Parser) -> None:
    # Here, not in tests/integration/conftest.py: options must be registered
    # by a conftest pytest loads before it parses the command line.
    parser.addoption(
        "--xmpp-lab", choices=("ejabberd", "prosody"), default="ejabberd",
        help="which lab the `agents` suites run against (default: ejabberd)",
    )
