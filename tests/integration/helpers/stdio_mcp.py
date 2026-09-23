"""A minimal MCP stdio client that behaves like Claude Code does for channels.

FastMCP's ``Client`` silently drops notification methods it doesn't know —
including ``notifications/claude/channel`` — so it can't observe channel
pushes. This helper spawns ``python -m xmpp_mcp`` as a subprocess and speaks
raw newline-delimited JSON-RPC over its stdio, exactly as Claude Code does:
``initialize`` → ``notifications/initialized`` → tool calls, while a reader
task collects every channel notification into a queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

CHANNEL_METHOD = "notifications/claude/channel"
# Env prefixes the helper strips from the inherited environment, so a
# developer's shell (or a real Claude Code session's CLAUDE_CODE_SESSION_ID)
# can't leak into the server under test.
_SCRUB = ("XMPP_", "OPENFIRE_", "WEBHOOK_", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
          "CLAUDE_CONFIG_DIR")


class ToolCallError(RuntimeError):
    """The server answered a tools/call with ``isError: true``."""


class StdioMCP:
    """One ``xmpp-mcp`` subprocess plus a JSON-RPC session over its stdio."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path) -> None:
        self.args = args
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB)}
        self.env.update(env)
        self.cwd = cwd
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.initialize_result: dict[str, Any] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=200)

    # --- lifecycle ------------------------------------------------------------

    async def start(self, timeout: float = 45.0) -> dict[str, Any]:
        """Spawn the server and complete the MCP handshake. Returns the initialize result."""
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "xmpp_mcp", *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
            limit=16 * 1024 * 1024,  # channel payloads can be large JSON lines
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        # The server connects to XMPP inside its lifespan, *before* it answers
        # initialize — hence the generous timeout.
        self.initialize_result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "xmpp-mcp-tests", "version": "0"},
            },
            timeout=timeout,
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.initialize_result

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            # Closing stdin is the MCP stdio shutdown signal; the lifespan then
            # leaves rooms and disconnects cleanly.
            with contextlib.suppress(Exception):
                self._proc.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        for t in self._tasks:
            t.cancel()

    async def __aenter__(self) -> "StdioMCP":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # --- JSON-RPC -------------------------------------------------------------

    def _write(self, msg: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> dict[str, Any]:
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"{method} timed out; server stderr:\n{self.stderr_tail()}"
            ) from None
        if "error" in reply:
            raise RuntimeError(f"{method} failed: {reply['error']}")
        return reply["result"]

    async def call(
        self, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> dict[str, Any]:
        """Call a tool; return its structured result (raises ToolCallError on isError)."""
        result = await self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout
        )
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            raise ToolCallError(text)
        if "structuredContent" in result:
            return result["structuredContent"]
        # Unstructured results carry the JSON in a text block instead.
        texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
        return json.loads(texts[0]) if texts else {}

    # --- channel events -------------------------------------------------------

    async def next_event(self, timeout: float = 10.0) -> dict[str, Any]:
        """Wait for the next ``notifications/claude/channel`` params."""
        try:
            return await asyncio.wait_for(self.events.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"no channel event within {timeout}s; server stderr:\n{self.stderr_tail()}"
            ) from None

    async def assert_no_event(self, within: float = 1.5) -> None:
        try:
            ev = await asyncio.wait_for(self.events.get(), timeout=within)
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"unexpected channel event: {ev}")

    def stderr_tail(self, n: int = 30) -> str:
        return "\n".join(list(self._stderr)[-n:])

    # --- readers --------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            msg = json.loads(line)
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == CHANNEL_METHOD:
                self.events.put_nowait(msg["params"])

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while line := await self._proc.stderr.readline():
            self._stderr.append(line.decode(errors="replace").rstrip())
