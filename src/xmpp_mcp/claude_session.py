"""Discover the Claude Code session this server belongs to, and watch its name.

Claude Code keeps one JSON file per running session in
``<config dir>/sessions/<pid>.json`` (config dir: ``$CLAUDE_CONFIG_DIR``, else
``~/.claude``). It carries, among other things::

    {"pid": 2194234, "sessionId": "7b3e9a41-…", "name": "Reviewer",
     "nameSource": "auto", "status": "idle", …}

Two parts matter here:

* ``sessionId`` — stable for the life of the session, globally unique: the
  natural **canonical** identity, and what the ``{session}`` JID placeholder
  expands to.
* ``name`` — the human-facing **friendly** name shown in Claude Code. It is
  *not* stable. Observed values of ``nameSource`` (from Claude Code 2.1.280):

  ``derived``    assigned at startup from the working directory
  ``auto``       generated later by Claude from what the session is doing
  ``user``       given explicitly (``claude --name``, ``CLAUDE_CODE_SESSION_NAME``,
                 or a manual rename)
  ``collision``  changed to resolve a clash with another session's name
  ``peer`` / ``hook``  set by another session or by a hook

  So a session typically starts as ``bridge-cse-…-18`` and later becomes
  ``Reviewer`` — which is why :class:`SessionWatcher` re-reads the file.

The file format is Claude Code's, undocumented and liable to change. Every
read here is defensive: a missing, unreadable or malformed file just means
"no session information", never a crash.

Finding *our* file. Claude Code sets ``CLAUDE_PID`` and
``CLAUDE_CODE_SESSION_ID`` for its shell commands, but — observed live with
2.1.280 — not for the MCP servers it spawns, and wrapper scripts can drop
them anyway. So, in order:

1. ``CLAUDE_PID`` → ``sessions/<pid>.json``;
2. walk up this process's ancestors (``/proc/<pid>/stat``) to the first one
   that has a session file — the Claude Code process that launched us;
3. scan ``sessions/*.json`` for a matching ``CLAUDE_CODE_SESSION_ID``.

When ``CLAUDE_CODE_SESSION_ID`` is known, a candidate whose ``sessionId``
disagrees is rejected, so a recycled PID can't make us adopt a stranger's
identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("xmpp_mcp.claude_session")

NAME_SOURCES = frozenset({"derived", "auto", "user", "collision", "peer", "hook"})


@dataclass(frozen=True)
class ClaudeSession:
    """What we use from a Claude Code session file."""

    path: Path
    pid: int | None
    session_id: str
    name: str | None
    name_source: str | None
    status: str | None
    how: str  # which discovery step found it, for the startup log
    # When Claude Code last wrote the file (its own `updatedAt`, ms since the
    # epoch). A status change is such a write, so on a change to idle this is
    # when the session went idle, to the millisecond, not to our poll.
    updated_at: float | None = None

    @property
    def mtime_ns(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return -1


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def read_session(path: Path, how: str = "path") -> ClaudeSession | None:
    """Parse one session file; ``None`` if it is missing or not usable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session_id = data.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    name = data.get("name")
    source = data.get("nameSource")
    status = data.get("status")
    pid = data.get("pid")
    updated = data.get("updatedAt")
    return ClaudeSession(
        path=path,
        pid=pid if isinstance(pid, int) else None,
        session_id=session_id.strip(),
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        # Unknown sources are passed on verbatim rather than dropped: the set
        # has grown before and a new value is still informative.
        name_source=source if isinstance(source, str) and source else None,
        status=status if isinstance(status, str) and status else None,
        how=how,
        updated_at=(updated / 1000 if isinstance(updated, (int, float))
                    and not isinstance(updated, bool) and updated > 0 else None),
    )


def _parent_pid(pid: int) -> int | None:
    """PPID from ``/proc/<pid>/stat`` (Linux). ``None`` where unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) is parenthesised and may contain spaces or
    # parentheses itself, so split after the *last* ")".
    try:
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def discover(
    env: Mapping[str, str] | None = None,
    *,
    pid: int | None = None,
    parent_of: Callable[[int], int | None] | None = None,
) -> ClaudeSession | None:
    """Find the session file for the Claude Code process that launched us."""
    env = os.environ if env is None else env
    sessions = config_dir(env) / "sessions"
    if not sessions.is_dir():
        return None
    expected = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None

    def accept(candidate: ClaudeSession | None) -> ClaudeSession | None:
        if candidate is None:
            return None
        if expected and candidate.session_id != expected:
            logger.debug("Ignoring %s: session %s is not ours (%s)",
                         candidate.path, candidate.session_id, expected)
            return None
        return candidate

    claude_pid = (env.get("CLAUDE_PID") or "").strip()
    if claude_pid.isdigit():
        found = accept(read_session(sessions / f"{claude_pid}.json", "CLAUDE_PID"))
        if found:
            return found

    walk = _walk_with(parent_of or _parent_pid)
    for ancestor in walk(pid if pid is not None else os.getpid()):
        found = accept(read_session(sessions / f"{ancestor}.json", "parent process"))
        if found:
            return found

    if expected:
        for path in sorted(sessions.glob("*.json")):
            found = read_session(path, "session id")
            if found and found.session_id == expected:
                return found
    return None


def _walk_with(parent_of: Callable[[int], int | None]) -> Callable[[int], Iterator[int]]:
    """Ancestors of a PID, nearest first, stepping up with ``parent_of``."""
    def walk(pid: int, limit: int = 64) -> Iterator[int]:
        seen: set[int] = set()
        current: int | None = pid
        while current and current > 1 and current not in seen and len(seen) < limit:
            seen.add(current)
            current = parent_of(current)
            if current and current > 1:
                yield current
    return walk


def resolve(setting: str, env: Mapping[str, str] | None = None) -> ClaudeSession | None:
    """Apply the ``XMPP_CLAUDE_SESSION`` setting: ``auto``, ``off`` or a file path."""
    value = (setting or "auto").strip()
    if value.lower() == "off":
        return None
    if value.lower() == "auto":
        return discover(env)
    return read_session(Path(value).expanduser(), "XMPP_CLAUDE_SESSION")


class SessionWatcher:
    """Re-read the session file when it changes; report name changes.

    Polls the file's mtime (cheap: one ``stat`` per interval) rather than
    using inotify, which is Linux-only and awkward inside some containers.
    """

    def __init__(
        self,
        session: ClaudeSession,
        on_change: Callable[[ClaudeSession, ClaudeSession], Any],
        interval: float = 10.0,
    ) -> None:
        self.session = session
        self._on_change = on_change
        self._interval = interval
        self._mtime = session.mtime_ns
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="xmpp-mcp-session-watch"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def poll(self) -> bool:
        """Check once; True if a change was reported."""
        mtime = self.session.mtime_ns
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        fresh = read_session(self.session.path, self.session.how)
        # A vanished or rewritten-for-another-session file is not a rename.
        if fresh is None or fresh.session_id != self.session.session_id:
            return False
        old, self.session = self.session, fresh
        if (old.name, old.name_source, old.status) == (fresh.name, fresh.name_source, fresh.status):
            return False
        try:
            result = self._on_change(old, fresh)
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)
        except Exception:  # noqa: BLE001 - a bad callback must not stop watching
            logger.exception("Session change handler failed")
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self.poll()
