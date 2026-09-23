# Contributing

Thanks for taking a look. This is a small, focused project — an MCP server over
XMPP. Bug reports, test cases, and support for more XMPP servers are all welcome.

## Dev setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

(On macOS/Linux use `.venv/bin/python` instead of `.venv\Scripts\python.exe`.)

## Running the tests

```powershell
# Fast unit tests — no network, no Docker (this is what CI runs).
.\.venv\Scripts\python.exe -m pytest -m "not docker and not integration"

# Full end-to-end suite — boots Openfire 4.8.1 in Docker and drives the server
# in-process. Requires Docker Desktop.
.\.venv\Scripts\python.exe -m pytest -m docker

# Wire-protocol subset — drives the built dist\xmpp-mcp.exe over real MCP stdio.
# Build it first (see below); the suite skips if the exe is missing or stale.
.\.venv\Scripts\python.exe -m pytest -m wire
```

Test markers (see `pyproject.toml`):

| Marker | What it needs |
|---|---|
| *(none)* | Pure unit tests — run anywhere. |
| `docker` | Docker Desktop; boots Openfire via docker compose. |
| `wire` | A current `dist\xmpp-mcp.exe` (subset of `docker`). |
| `agents` | Docker; channel / identity / relay suites against an agent lab: `pytest -m agents` (ejabberd) or `pytest -m agents --xmpp-lab prosody`. |
| `integration` | A live XMPP server you provide via env vars. |
| `llm` | `ANTHROPIC_API_KEY` — opt-in, real Claude session. |

## Local lab

```powershell
python start-lab.py            # Openfire: of_* admin tools work; MAM doesn't
python start-lab-ejabberd.py   # ejabberd: MAM works; no Openfire REST
```

Copy `.mcp.json.example` to `.mcp.json` (gitignored) to point Claude Code /
Claude Desktop at the lab. See `docs/CLAUDE_CODE_SETUP.md`.

## Building the standalone binary

```powershell
.\.venv\Scripts\pyinstaller.exe xmpp-mcp.spec   # -> dist\xmpp-mcp.exe (~32 MB)
```

## Conventions

- All XMPP / Openfire ops raise `XMPPError` / `OpenfireError` at the wrapper
  layer; tools catch and re-raise as `fastmcp.exceptions.ToolError`.
- Tool modules expose `register(mcp)` and are wired in `server.py`.
- MCP responses exchange `DataForm` dicts, never raw XML.
- `CLAUDE.md` documents the architecture and a list of server-specific gotchas
  worth reading before changing the XMPP or pubsub layers.
- Channel mode, per-agent identities and the webhook relay are documented in
  `docs/CHANNELS.md`. Channel notifications can only be observed over real
  stdio JSON-RPC (an in-process MCP client drops unknown methods) — see
  `tests/integration/helpers/stdio_mcp.py`.

Please run the unit tests before opening a PR; add a test for any behaviour you
change.
