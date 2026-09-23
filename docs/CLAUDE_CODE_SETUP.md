# Pointing Claude Code (or Claude Desktop) at the lab

> Wiring Claude Code sessions to each other as **agents** over XMPP (pushed
> messages via the channels API, per-session identities, the webhook relay)
> is a different setup — see [CHANNELS.md](CHANNELS.md) and
> `.mcp.json.channel.example`. This page is the plain tool-server setup.

Once the dependencies are installed and the Openfire lab is running, you can
have a real Claude session use the MCP server — call tools in natural language,
search rooms, publish forms, the lot.

## Prerequisites

```powershell
# 1. Install into the project venv. Running from source needs no PyInstaller
#    build and always reflects the current code.
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. Bring up the lab — pick one
python start-lab.py            # Openfire: of_* admin tools work; MAM doesn't
python start-lab-ejabberd.py   # ejabberd: MAM works; no Openfire REST
```

> A frozen `dist\xmpp-mcp.exe` (built via `pyinstaller xmpp-mcp.spec`) is only
> needed to run on a machine without Python — point `command` at it instead of
> the venv Python shown below.

**Use ejabberd for cross-session history work.** If you want Claude to answer
"what was said in #r1 yesterday" *after* restarting the bot, you need MAM,
and the ejabberd lab is where MAM actually returns results.

Use Openfire only if you want to exercise the `of_*` admin tools (Openfire's
REST API, which ejabberd doesn't expose). Both labs use the **same XMPP
domain (`xmpp.test`), same accounts (`bot`/`alice`/`bob`/`carol`/`admin`)
and same ports** — `.mcp.json` doesn't change.

> Only one lab at a time — they share the same host ports. Run
> `docker compose -f tests\integration\docker\docker-compose.yml down -v`
> (or the ejabberd compose) before switching.

The container exposes:

| Service | Port |
|---|---|
| XMPP C2S (STARTTLS) | 5222 |
| XMPP S2S            | 5269 |
| XMPP component XEP-0114 | 5275 |
| Admin console + REST API | 9090 |

## Claude Code (CLI)

Copy `.mcp.json.example` to `.mcp.json` (gitignored) at the repo root, or add
the entry to your user-level `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "xmpp": {
      "command": ".venv\\Scripts\\python.exe",
      "args": ["run-server.py"],
      "env": {
        "XMPP_JID": "bot@xmpp.test",
        "XMPP_PASSWORD": "botpw",
        "XMPP_HOST": "127.0.0.1",
        "XMPP_PORT": "5222",
        "XMPP_TLS_INSECURE": "true",
        "XMPP_NICK": "xmpp-mcp",
        "OPENFIRE_BASE_URL": "http://127.0.0.1:9090",
        "OPENFIRE_ADMIN_USER": "admin",
        "OPENFIRE_ADMIN_PASSWORD": "adminpw"
      }
    }
  }
}
```

Paths are relative to the repo root (where Claude Code launches a project-scoped
MCP server). For a user-level `~/.claude.json`, use absolute paths. To run the
frozen binary instead, set `"command": "dist\\xmpp-mcp.exe", "args": []`.

Start Claude Code in that directory:

```powershell
claude
```

The first time, Claude Code asks you to trust the project's MCP servers — say
yes. From then on, the bot's tools (`send_message`, `search_messages`,
`pubsub_*`, `mam_query`, `of_*`, …) are available in conversation.

Verify with:

```
> what xmpp tools do you have?
```

Claude will list everything the server registered.

## Claude Desktop

Same JSON, different location: `%APPDATA%\Claude\claude_desktop_config.json`.
Restart Claude Desktop. The bot appears under the connectors / MCP menu.

## What you can ask Claude (natural language)

The tool set is rich enough that the model can fulfil most prompts without
follow-up questions. Examples that work after running
`scripts\demo_medevac.py` once:

* *"Who's been talking about medical evacuation across the rooms?"*
  → calls `search_messages(query="medevac")` + `search_messages(query="medical evacuation")`,
    summarises by speaker and room.
* *"Read the 9-line that was just published and tell me the precedence."*
  → calls `pubsub_read_forms(service="pubsub.xmpp.test", node="medevac-9line")`,
    decodes the form, reports `Line 3: A — Urgent`.
* *"Set up a status board: create a pubsub node called sitrep, give alice publish
  rights, and subscribe yourself."*
  → chains `pubsub_create_node` + `pubsub_set_affiliations` + `pubsub_subscribe`.
* *"Push the medevac summary into the #medical-ops room."*
  → calls `pubsub_get_recent_events(node="medevac-9line", kind="publish")` +
    `send_room_message(room_jid=...)` — the bridge from pubsub to MUC.

The tool descriptions are written for an LLM (verbose, with `Field`
descriptions on every parameter) — Claude doesn't usually need to ask
follow-ups.

## What persists across restarts

| You restart… | What survives |
|---|---|
| **The MCP server (xmpp-mcp.exe)** | Rooms, room members, pubsub nodes, pubsub items, and (on ejabberd) MUC message history queryable via `mam_query`. **Not** the in-memory `search_messages` buffer or the `pubsub_get_recent_events` buffer — bot has to be present to capture live traffic. |
| **The container** (`docker compose restart`) | Everything above (writable layer + named volume persist). |
| **`docker compose down`** (no `-v`) | Everything — both labs now mount a named volume. |
| **`docker compose down -v`** | Nothing — explicit volume removal. |

On ejabberd, "show me what was said in #r1 yesterday" works via `mam_query`
even after restarting the MCP server.

## MAM (cross-session history)

`mam_query` returns historical messages from `mod_mam`'s archive — including
messages from before the bot's current session even started.

* **ejabberd lab** (`start-lab-ejabberd.py`): MAM works. Verified end-to-end —
  alice sends to r1, disconnects; fresh bot starts, calls `mam_query(room=r1)`,
  gets back the messages with `query="medevac"` filtering working.
* **Openfire lab** (`start-lab.py`): MAM silently times out. `tests/integration/test_mam_e2e.py`
  marks the round-trip test `@xfail(strict=False)` with the full Openfire
  Monitoring 2.6.1 diagnosis. Workaround on Openfire is to keep the bot
  continuously running and use `search_messages` (in-memory buffer).
