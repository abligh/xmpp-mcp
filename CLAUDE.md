# xmpp-mcp

An MCP server that lets an LLM operate over XMPP — direct and group chat,
roster/presence, service discovery, **XEP-0258 security labels** (Isode
M-Link), full **XEP-0060 pubsub + XEP-0004 data forms**, plus admin over the
Openfire REST API plugin.

Also a **Claude Code channel** (`--channel`): one server per Claude session
turns that session into an addressable agent on a shared XMPP server, with
inbound messages *pushed* into the session (no polling) — plus a standalone
**webhook→XMPP relay**. See `docs/CHANNELS.md`.

Targets two XMPP server products in particular: **Openfire** (open-source) and
**Isode M-Link** (commercial, military-grade). The core messaging surface is
RFC 6120/6121 so it also works against ejabberd, Prosody, etc.

## Stack & layout

Python 3.11+ • FastMCP 4.x (`fastmcp>=4,<5`; channel mode needs its
`experimental_capabilities=` and notification middleware) • slixmpp (1.15+) •
httpx • pydantic-settings • aiohttp (the webhook relay, `[webhook]` extra).

```
src/xmpp_mcp/
  __init__.py
  __main__.py            # `python -m xmpp_mcp` entry; PyInstaller entry too
  server.py              # FastMCP app, lifespan, tool registration, main()
  config.py              # pydantic-settings Settings (XMPP_* + OPENFIRE_*)
  xmpp_client.py         # slixmpp ClientXMPP wrapper, inbox deque,
                         #   pubsub event buffer, pubsub property
  channel.py             # Claude Code channel: notification model, sender
                         #   gate, queue/pump, session-binding middleware
  identity.py            # {session}/{agent}/{host}/{fqdn} JID templating
  claude_session.py      # find + watch the Claude Code session file
                         #   (session ID -> canonical JID, name -> friendly name)
  agents.py              # <agent/> presence extension + presence cache
  webhook_relay/         # standalone HTTP→XMPP relay (aiohttp + slixmpp)
    __init__.py          #   public surface + CLI entry point
    settings.py          #   WEBHOOK_* settings
    routing.py           #   which JID(s) a request targets: explicit,
                         #     envelope, route table, defaults
    routes.py            #   the operator's TOML route table
    stanza.py            #   scrubbing + byte budget for arbitrary payloads
    auth.py              #   may this caller inject a message at all?
    relay.py             #   XMPP connection, delivery queue, HTTP endpoint
    providers/           #   THE source-specific layer: github, gitlab, generic
                         #     (how to authenticate and summarise one sender)
  data_forms.py          # XEP-0004 DataForm / FormField TypedDicts;
                         #   parse_form, build_form_element, build_submit_form
  security_labels.py     # XEP-0258 catalog fetch + label builder (M-Link)
  pubsub.py              # PubSubClient — async wrapper over slixmpp xep_0060
  mam.py                 # MAMClient — XEP-0313 archive queries (xfail on this lab,
                         #   see gotcha #16)
  openfire_admin.py      # Openfire REST API client (httpx)
  tools/
    __init__.py          # CTX_* keys, get_xmpp / get_settings / get_openfire
    messaging.py         # send_message, get_recent_messages, search_messages
    muc.py               # join_room, leave_room, send_room_message, ...
    presence.py          # set_presence, get_roster, add/remove_contact + resource
    disco.py             # discover_features, list_security_labels + resource
    pubsub.py            # 23 pubsub tools (see "Pubsub surface" below)
    mam.py               # mam_query — XEP-0313 historical room queries
    agents.py            # reply, list_agents, get_identity
    admin.py             # Openfire of_* tools

tests/                   # unit tests (no network)
  test_config.py
  test_data_forms.py
  test_search_inbox.py
  test_security_labels.py
  test_openfire_admin.py
  test_pubsub_events.py
  test_identity.py           # JID templating / normalisation
  test_channel.py            # gate, payload, pump, middleware, server wiring
  test_agents.py             # presence extension, cache, list_agents
  test_webhook_relay.py      # relay core: routing, stanza safety, HTTP, delivery
  test_webhook_providers.py  # per-sender auth + summaries; no-downgrade rule
  test_webhook_routing.py    # envelope, route table, names, per-provider dedupe
  test_claude_session.py     # session discovery/watching, renames, addressing
  test_muc_client.py         # MUC bookkeeping: nicks, room keys, occupants
  test_xmpp_client.py        # opt-in `integration` marker, needs live server

tests/integration/       # docker-based E2E (two labs available — see below)
  conftest.py            # session-scoped openfire fixture (compose up/down)
                         #   + function-scoped mcp / raw_* / seclabel_component
  docker/
    Dockerfile.openfire  # nasqueron/openfire:4.8.1 + REST API plugin + autosetup
    openfire.xml         # <autosetup/> pre-creates bot/alice/bob/carol
    docker-compose.yml   # project name xmpp-mcp-test; exposes 5222/5269/5275/9090
    ejabberd/            # alternative lab — MAM works, and the lab the
                         #   channel/agent suites need (open XEP-0077
                         #   registration + non-anonymous rooms)
      docker-compose.yml # project name xmpp-mcp-ej; same ports + 5280 admin
      ejabberd.yml       # mod_mam, mod_muc (mam=true, anonymous=false),
                         #   mod_register + registration_timeout: infinity
  helpers/
    raw_client.py        # RawXMPPClient — slixmpp wrapper for the "other side"
    chat_script.py       # ChatScript — scripted multi-room conversations
    seclabel_component.py # SecurityLabelComponent — XEP-0114 stub of M-Link's
                         #   XEP-0258 catalog (on seclabel.xmpp.test)
    stdio_mcp.py         # raw JSON-RPC stdio client — the only way to observe
                         #   channel notifications (see gotcha #20)
  test_smoke.py
  test_messaging_e2e.py
  test_muc_e2e.py
  test_presence_e2e.py
  test_discovery_e2e.py
  test_openfire_admin_e2e.py
  test_chat_search_e2e.py        # scripted chat → search_messages assertions
  test_pubsub_e2e.py             # 15 tests: nodes, forms, raw, events, niche ops
  test_security_labels_e2e.py    # 4 tests vs the XEP-0258 stub component
  test_mcp_wire_e2e.py           # 4 tests over real stdio JSON-RPC to dist\\xmpp-mcp.exe
  test_resilience_e2e.py         # 3 tests — docker pause/unpause survival
  test_llm_driven_e2e.py         # 1 test — real Claude session via Anthropic SDK
                                 #   (opt-in: needs ANTHROPIC_API_KEY)
  test_channel_e2e.py            # 13 tests — `ejabberd` marker: channel push,
                                 #   reply, MUC echo suppression, gate, agents
  test_webhook_relay_e2e.py      # 5 tests — HTTP → relay → agent channel
  test_channel_resilience_e2e.py # 1 test  — reconnect + re-join after restart
  test_identity_e2e.py           # 6 tests — canonical JIDs, friendly names,
                                 #   live renames, name-routed webhooks

scripts/                 # one-off demo runners (use the test fixtures + an
  demo_chat_search.py    #   in-process FastMCP Client)
  demo_pubsub_forms.py
```

## MCP tool surface (`pytest`-verified)

**Messaging / MUC / presence / discovery / admin** — see `src/xmpp_mcp/tools/`.

Notably:

- `reply(to, message, thread?)` — answer a channel message; routes to
  groupchat for a joined room, 1:1 otherwise.
- `list_agents(include_offline?, agents_only?)` — the XMPP equivalent of
  Claude Code's `ListAgents`: roster contacts + occupants of joined rooms,
  each with canonical JID, internal agent ID, human-facing name and presence.
- `get_identity()` — this agent's own JID / name / ID / rooms.

- `search_messages(query?, room?, participant?, since?, limit?)` — non-destructive
  search over the inbox. Powers "who said X about Y" workflows. Inbox records
  carry `room` and `nick` derived fields for clean filtering.

### Pubsub surface (23 tools — full XEP-0060 coverage)

| Area | Tools |
|---|---|
| Nodes | `pubsub_list_nodes`, `pubsub_create_node` (with optional `config_values`), `pubsub_delete_node` |
| Config | `pubsub_get_node_config`, `pubsub_configure_node` |
| Forms | `pubsub_publish_form_template`, `pubsub_submit_form`, `pubsub_read_forms`, `pubsub_get_item` |
| Raw payloads | `pubsub_publish_raw`, `pubsub_get_items_raw`, `pubsub_get_item` (auto-detects form vs raw) |
| Item ops | `pubsub_retract_item`, `pubsub_purge_node` |
| Subscriptions | `pubsub_subscribe`, `pubsub_unsubscribe`, `pubsub_list_subscriptions`, `pubsub_list_node_subscriptions`, `pubsub_get_subscription_options` (incl. `defaults=True`), `pubsub_set_subscription_options` |
| Affiliations | `pubsub_list_my_affiliations`, `pubsub_list_node_affiliations`, `pubsub_set_affiliations` |
| **Live events** | `pubsub_get_recent_events(node?, kind?, limit?)` — drains buffered pubsub-event notifications captured by slixmpp handlers; required for "react to new fills" workflows |

All form payloads exchange the `DataForm` shape from `xmpp_mcp.data_forms`
(`{type, title?, instructions?, fields[]}`) — never raw XML in the API. The
escape hatch is `pubsub_publish_raw` / `pubsub_get_items_raw` for non-form
items (PEP, ATOM, JSON-in-pubsub).

## Running

```powershell
# install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# unit tests (fast, no network) — ~300 tests, a few seconds
.\.venv\Scripts\python.exe -m pytest -m "not docker and not integration"

# full docker-based E2E suite (boots Openfire) — ~57 tests, ~75s
.\.venv\Scripts\python.exe -m pytest -m docker

# wire-protocol tests against the built .exe (subset of docker)
.\.venv\Scripts\pyinstaller.exe xmpp-mcp.spec   # rebuild if stale!
.\.venv\Scripts\python.exe -m pytest -m wire

# channel / multi-agent suite — needs the ejabberd lab, NOT Openfire.
# Must run on its own: the two labs bind the same ports (conftest deselects
# these whenever Openfire tests are selected too).
.\.venv\Scripts\python.exe -m pytest -m ejabberd

# bring up the lab — choose the right server for the job
python start-lab.py              # Openfire (legacy, of_* admin works,
                                 #   MAM doesn't — see gotcha #16)
python start-lab-ejabberd.py     # ejabberd 25.04 (MAM works, no Openfire REST,
                                 #   open registration + non-anonymous rooms —
                                 #   what the channel/agent suites need)

# real-LLM tests (opt-in, costs API tokens, needs ANTHROPIC_API_KEY)
$env:ANTHROPIC_API_KEY = "sk-…"
.\.venv\Scripts\python.exe -m pytest -m llm

# everything
.\.venv\Scripts\python.exe -m pytest

# build standalone Windows .exe (no runtime needed on target)
.\.venv\Scripts\pyinstaller.exe xmpp-mcp.spec   # → dist\xmpp-mcp.exe (~32 MB)

# run the server
.\.venv\Scripts\xmpp-mcp.exe                    # stdio MCP transport
python -m xmpp_mcp                              # same thing from source

# lab demos
python scripts\demo_chat_search.py              # multi-room chat + search_messages
python scripts\demo_pubsub_forms.py             # publish form, 3 fills, read back
```

Channel mode (see `docs/CHANNELS.md` for the whole story):

```powershell
# one agent per Claude Code session
xmpp-mcp --channel --agent-name reviewer --register --join agents@conference.xmpp.test
claude --dangerously-load-development-channels server:xmpp

# the global webhook→XMPP relay (one per host)
xmpp-webhook-relay                      # WEBHOOK_* env; HTTP on 127.0.0.1:8788
```

Required env vars: `XMPP_JID`, `XMPP_PASSWORD`. Optional: `XMPP_HOST`,
`XMPP_PORT`, `XMPP_TLS_INSECURE`, `XMPP_NICK`, `OPENFIRE_BASE_URL` +
`OPENFIRE_ADMIN_USER` / `OPENFIRE_ADMIN_PASSWORD` (enables `of_*` tools).
Channel/agent mode adds `XMPP_CHANNEL`, `XMPP_AGENT_NAME`, `XMPP_AGENT_ID`,
`XMPP_DISPLAY_NAME`, `XMPP_AGENT_HOST`, `XMPP_AUTO_JOIN`,
`XMPP_CHANNEL_ALLOW`, `XMPP_REGISTER`; the relay reads `WEBHOOK_*`. See
`.env.example`.

## Architectural conventions

- **All XMPP / Openfire ops raise `XMPPError` / `OpenfireError`** at the
  wrapper layer (`xmpp_client.py`, `pubsub.py`, `openfire_admin.py`). Tools
  catch those and re-raise as `fastmcp.exceptions.ToolError` — anything else
  leaks as an unhelpful stacktrace to the client.
- **Tool modules expose `register(mcp)`** and are wired in `server.py`.
- **Shared state lives in `ctx.lifespan_context`** (keys: `xmpp`, `settings`,
  `openfire`). Helpers in `tools/__init__.py`: `get_xmpp`, `get_settings`,
  `get_openfire` (the last raises a clean ToolError when not configured).
- **Settings are loaded in `create_server()`**, not the lifespan: channel mode
  changes what the server declares at `initialize` (capability + instructions).
  `create_server(**overrides)` takes CLI-flag overrides; `None` values are
  ignored so an unset flag never masks an env var.
- **Channel pushes go through `ChannelBridge`**, never straight from a slixmpp
  handler: the handler only enqueues (sync, non-blocking), a pump task awaits
  the MCP session. The inbox deque is still filled for `get_recent_messages`
  / `search_messages`, whether or not channel mode is on.
- **Tools that touch slixmpp state are `async`**, so FastMCP runs them on the
  event loop; a sync tool runs in a worker thread and races the XML stream.
- **Addresses: JIDs route, names resolve.** Anything with an `@` is a JID;
  anything else goes through `XMPPClient.resolve_address` (friendly name,
  agent ID, roster name or nick — exactly one match, or an error).
- **Every JID a tool accepts is parsed** with `xmpp_client.parse_jid`, and
  anything returned in a tool result is a `str` — never a slixmpp `JID`
  object (gotcha #22).
- **DataForm dicts, never raw XML, in MCP responses.** Returning a
  `TypedDict` directly from a FastMCP tool wraps it in a Pydantic root model
  on the client side (non-subscriptable); declare return type as
  `dict[str, Any]` while still building the typed shape internally.

## Gotchas already learned (don't re-discover)

1. **slixmpp `enable_direct_tls` footgun.** When you pin a custom host/port,
   slixmpp's default tries direct TLS on that port *before* STARTTLS. On the
   standard C2S port 5222 the ClientHello bytes hit the server's plain XML
   parser → connection reset, no useful error. Always set
   `xmpp.enable_direct_tls = False` when `XMPP_HOST` is given. (Already
   handled in `XMPPClient.__init__` and `RawXMPPClient.__init__`.)
2. **slixmpp `connect()` signature changed.** 1.15.0 is `connect(host, port)`,
   *not* `connect(address=(host, port))`.
3. **FastMCP banner corrupts stdio.** `create_server().run(show_banner=False)`
   — stdout is reserved for the MCP protocol on stdio transport.
4. **slixmpp double-handler on MUC.** Subscribing to both `message` AND
   `groupchat_message` double-counts every MUC line; subscribe to `message`
   only (it fires for groupchat too).
5. **MUC history on join.** `join_muc_wait(maxstanzas=0)` to opt out — without
   it, every new connection back-fills the inbox with the room's recent
   history and `search_messages` returns duplicates.
6. **slixmpp `pubsub_publish` fires per-item.** For multi-item messages it
   fires once per item with the *full* msg attached every time; dedupe by
   `msg["id"]` to process all items exactly once. (Done in
   `XMPPClient._on_pubsub_items` via `_seen_pubsub_msg_ids`.)
7. **Openfire pubsub default `max_items=1` + `persist_items=false`.** Without
   overriding these at node creation, publishing a second item silently evicts
   the first. The form template vanishes as soon as someone submits. Tests
   that need persistence pass `config_values={"pubsub#persist_items": True,
   "pubsub#max_items": "100"}` to `pubsub_create_node`.
8. **Openfire REST API is disabled by default.** Needs two DB-backed system
   properties: `adminConsole.access.allow-wildcards-in-excludes=true` (the
   actual gate — without it AuthCheckFilter strips the plugin's URL
   exclusions) **and** `plugin.restapi.enabled=true`. Openfire has no
   facility to seed DB-backed system properties at first boot, so the test
   fixture drives the admin-console session form (`tests/integration/conftest.py
   _enable_restapi`). Same handshake is needed for any deployment.
9. **Openfire pubsub purge keeps the last item.** Even after `purge`, one
   "last-published" item may remain (configurable per node). Tests assert
   `count < before and count <= 1`, not strict equality with zero.
10. **Openfire doesn't implement per-node default subscription options.**
    `pubsub_get_subscription_options(defaults=True)` returns a clean
    `ToolError` against Openfire; works against ejabberd / Prosody / M-Link.
11. **Openfire AUTO_SETUP for embedded DB.** The autosetup `<database>` block
    only handles the "standard" (external JDBC) path. For embedded HSQLDB,
    put `<connectionProvider><className>...EmbeddedConnectionProvider</className></connectionProvider>`
    at the **top level** of `<jive>`, alongside `<autosetup>`, not inside it.
    See `tests/integration/docker/openfire.xml`.
12. **Openfire external components need three DB-backed properties.** Same
    enable-handshake as the REST API plugin: `xmpp.component.socket.active=true`,
    `xmpp.component.socket.port=5275`, `xmpp.component.defaultSecret=…`. Set
    in `conftest.py _enable_restapi`. The port doesn't bind until *after*
    these are set, so `_wait_for_tcp(host, 5275)` is required before
    connecting a component.
13. **Async fixture scope vs test loop scope.** pytest-asyncio in auto mode
    runs each test in its own event loop. A `@pytest_asyncio.fixture(scope="session")`
    that connects an XMPP/component over the network ends up with sockets
    registered in the *session* loop — the test's loop won't poll them, so
    every IQ times out. **Function-scope any async fixture that holds a live
    socket.** The seclabel_component fixture is function-scoped for this
    reason (slow but correct).
14. **slixmpp reconnect backoff is exponential and uncapped at the test level.**
    `reschedule_connection_attempt` does `_connect_loop_wait = min(300, n*2+1)`
    so a 7-second Openfire restart can land in a 15–31s wait window. The bot
    *will* reconnect, just not quickly. The resilience tests use
    `docker pause`/`unpause` instead — TCP preserved, no backoff involved.
    For production fast-reconnect, override `reschedule_connection_attempt`
    to cap the wait at a few seconds.
15. **FastMCP wire transport keeps subprocess alive by default.**
    `StdioTransport(..., keep_alive=True)` (the default) preserves the
    subprocess between `async with Client(...)` blocks. Pass `keep_alive=False`
    in test fixtures so each test gets a clean subprocess.
16. **Openfire Monitoring plugin 2.6.1 silently drops MUC MAM queries in
    this lab.** With every documented property set (`conversation.metadataArchiving`,
    `messageArchiving`, `roomArchiving`, `roomArchivingStanzas` all true, room
    created via REST with `logEnabled=true`), MAM IQs addressed to a room JID
    time out with no server response. Wire/disco shows `urn:xmpp:mam:2`
    supported, the IQ format is correct, but no `<result>`/`<fin>` ever comes
    back. Sometimes returns 404 "archive not found" instead. The
    `mam_query` tool is correctly wired — **the workaround is the ejabberd lab**
    (`python start-lab-ejabberd.py`), where MAM returns results immediately.
17. **`failed_auth` fires per-mechanism; use `failed_all_auth` instead.**
    slixmpp tries SASL mechanisms in order (SCRAM-SHA-1 → X-OAUTH2 → PLAIN
    typically) and fires `failed_auth` after each one. Treating the first
    failure as final tears down the connection mid-fallback — ejabberd's
    SCRAM-SHA-1 channel-binding rejects slixmpp's `c=y,,` flag, then PLAIN
    succeeds, but the disconnect cancels everything. Hook `failed_all_auth`
    (post-exhaustion) instead.
18. **SASL over plain TCP requires explicit opt-in.** slixmpp refuses PLAIN
    and SCRAM-SHA-1 over unencrypted streams by default. The ejabberd lab
    serves SASL on plain 5222 (no TLS configured), so the bot has to set
    `self.xmpp["feature_mechanisms"].unencrypted_plain = True` and
    `unencrypted_scram = True`. Gated on `XMPP_TLS_INSECURE=true` so it's
    lab-only behaviour, never production.
19. **ejabberd `auth_password_format: scram` + `ejabberdctl register`** —
    works. The CLI accepts plaintext passwords and the server stores the
    SCRAM-derived secrets internally. No special bootstrap needed beyond
    `ejabberdctl register <user> <domain> <password>`.
20. **MCP clients drop unknown notification methods — test channels on the
    wire.** `notifications/claude/channel` is a Claude Code extension, not a
    core MCP type, so the FastMCP/`mcp` client parses it, finds no binding and
    discards it: an in-process `fastmcp.Client` can never observe a channel
    push. The e2e suite therefore drives `python -m xmpp_mcp` as a subprocess
    and speaks raw newline-delimited JSON-RPC over its stdio
    (`tests/integration/helpers/stdio_mcp.py`), exactly like Claude Code.
    Related: `fastmcp.Client` defaults to the modern (2026-07-28) protocol,
    which has **no `initialize` handshake at all** — pass `mode="legacy"` to
    exercise the handshake path. Claude Code likewise refuses to register a
    channel server that negotiates 2026-07-28, so don't set
    `MCP_PROTOCOL_NEGOTIATION=auto` for agent sessions.
21. **Notifications may only be pushed after `notifications/initialized`.**
    The session's standalone outbound channel isn't usable before the client
    finishes initialising, and the MCP lifecycle forbids it. `ChannelBridge`
    binds in a FastMCP notification middleware at exactly that point and
    queues anything that arrives earlier (XMPP connects during the lifespan,
    i.e. *before* initialize, so offline messages routinely land first).
22. **slixmpp hands back JID objects, which break structured tool output.**
    `xep_0045.get_jid_property(room, nick, "jid")` returns a `JID`, not a
    `str`. Returned as-is from a tool, FastMCP can't serialise the result and
    silently emits text content only — and a strict client then fails with
    "has an output schema but did not return structured content". Only
    disclosed-real-JID (non-anonymous) rooms hit it, which is why it stayed
    hidden until the ejabberd lab. Stringify at the boundary.
23. **MUC presence stops firing the generic `presence` event.** After an
    occupant's first presence, `xep_0045._handle_presence` sets
    `ignore_updates` on it, and `basexmpp._handle_presence` then returns early
    — so a presence cache built only on `presence` goes stale for rooms.
    Subscribe to `groupchat_presence` as well (updates are idempotent).
24. **`client_roster` is not the roster.** slixmpp creates a roster entry for
    *any* JID that sends presence, including MUC rooms, so iterating it
    invents contacts. Track real membership from the `roster_update` event
    (the roster result plus RFC 6121 §2.1.6 pushes) instead.
25. **A presence broadcast doesn't reach rooms.** RFC 6121 §4.4 broadcast
    presence goes to roster contacts only; room occupants see a change only if
    it is *also* sent as directed presence to `room@service/nick`
    (XEP-0045 §7.7). `set_presence` sends both.
26. **slixmpp never reconnects by itself.** `connection_lost` fires
    `disconnected` and stops; only an explicit `connect()` brings the stream
    back. Both the agent client and the relay reconnect on `disconnected`
    with 1s→30s backoff, and the agent re-joins its rooms on the new session
    (occupancy never survives a stream).
27. **ejabberd `mod_register` config traps.** `welcome_message` must be a map,
    not a string — passing `none` makes the server refuse to start. And
    registrations are rate-limited to one per source IP per 600 s by default
    (`registration_timeout: infinity` in the lab), which every agent in a
    container hits because they all share the docker bridge IP.

28. **`fnmatch` globs cross `@` and `/` — never match a full JID against an
    account pattern.** A resourcepart may legally contain `@` (RFC 7622
    opaquestring) and a MUC nick is peer-chosen, so `*@example.com` is
    satisfied by `mallory@evil.test/spoof@example.com` or by an occupant who
    takes the nick `alice@example.com`. `SenderGate` therefore matches
    account patterns (no `/`) against the **bare** JID only, and occupant
    patterns (with `/`) against the occupant JID only.
29. **A payload can kill the XML stream.** Characters XML 1.0 §2.2 forbids
    (most C0 controls, lone surrogates) serialise raw into a stanza and make
    the server's parser abort the connection — every queued message is lost
    and `msg.send()` raises nothing, so it looks like success. Anything
    reaching a stanza body from outside is scrubbed (`webhook_relay.stanza.scrub`).
    Budget stanza size in **bytes after XML escaping**, not characters: one
    emoji is 4 bytes and `&` becomes 5.
30. **`hmac.compare_digest` raises TypeError on non-ASCII `str`.** aiohttp
    hands header values over as latin-1 text, so comparing a secret straight
    from a header turns any auth check into a 500 for an attacker who sends a
    high byte. Encode both sides to bytes first.
31. **slixmpp writes straight to the asyncio transport.** There is no send
    queue to inspect (`send_queue` does not exist in 1.17): "the stanza is on
    the wire" means `transport.get_write_buffer_size()` reached 0 while the
    session was still up. A stanza handed over to a dying stream is dropped
    silently, so "no exception" must not be counted as delivered.
32. **`disconnect()` does not stop slixmpp's connect loop.** It only cancels
    an in-flight attempt when a transport exists, so a `stop()` issued while
    slixmpp is retrying leaves the loop running — it can succeed later and
    resurrect a "stopped" client that peers still see online. Call
    `cancel_connection_attempt()`, and bound the `disconnect()` await: it
    waits for a stream close that never comes if you were never connected.
33. **`asyncio.wait_for` cancels the future it waits on.** After a `start()`
    timeout, `self._ready.done()` is True but `.exception()` raises
    `CancelledError` — a `BaseException` that slixmpp's event dispatch does
    not contain, so it escapes the handler. Check `.cancelled()` first.
34. **The room, not the client, decides the MUC nick.** XEP-0045 §7.2.9 lets
    the service assign one, JID prep can fold the requested one (NFD vs NFC),
    and status 303 renames later. slixmpp tracks the truth in
    `xep_0045.our_nicks`; a client that keeps the nick it *asked* for will
    fail to recognise its own reflected messages — which, in channel mode,
    means pushing its own room posts back into its own session. Track code
    110 self-presence.
35. **Canonicalise room JIDs in one place.** JIDs compare case-insensitively,
    so if `join_room` preps the key but `send_groupchat` / `leave_room` do
    raw dict lookups, `join_room("Room@Conf.X")` succeeds and every later
    call with the same string reports "not joined" (`room_key`).
36. **A slixmpp stanza is its own iterator — never iterate one in a handler.**
    `ElementBase.__iter__` returns `self` and resets an index stored on the
    stanza. xep_0060 fires `pubsub_publish` *synchronously from inside its own
    loop over the items*, so a handler that also iterates
    `msg["pubsub_event"]["items"]` rewinds that loop and gets fired again, for
    ever — the event loop spins at 100% and eventually segfaults. A dedupe on
    `msg["id"]` hid it, until ejabberd sent PEP notifications, which carry no
    id (RFC 6120 makes it optional). Iterate `.iterables` (a plain list), and
    recognise repeat fires by stanza identity.
37. **In pydantic v2 a validator's own assignment marks a field as set.** After
    `self.xmpp_nick = …` in a `model_validator`, `"xmpp_nick" in
    model_fields_set` is True, so "was it configured?" can no longer be asked.
    Record it in a `PrivateAttr` *before* defaulting (`nick_is_explicit`) —
    otherwise room nicks silently never follow a rename.
38. **After a MUC nick change, ejabberd re-broadcasts the presence you joined
    with.** The status-303 dance moves the nick, but occupants see the old
    extension elements (here, the old `<agent name>`). Send a status update
    under the new nick once 303 arrives.
39. **The Claude Code session file is undocumented and changes over time.**
    `~/.claude/sessions/<pid>.json`: `sessionId` is stable, `name` is not —
    `nameSource` goes `derived` (from the cwd) → `auto` (Claude names it) or
    `user` (`--name`, `CLAUDE_CODE_SESSION_NAME`, a rename), plus
    `collision`, `peer`, `hook`. Read it defensively, watch it, and never build
    a canonical address from the name. Unit tests pin `XMPP_CLAUDE_SESSION=off`
    (tests/conftest.py), because under Claude Code the suite would otherwise
    adopt the developer's own session.

## Test markers

- `not docker and not integration` → fast unit tests, no network/Docker
- `docker` → boots Openfire container; superset of `wire`
- `ejabberd` → channel / multi-agent suites against the ejabberd lab. Also
  marked `docker` (so unit runs skip them), but deselected automatically when
  Openfire tests are selected — run `pytest -m ejabberd` on its own
- `wire` → drives `dist\xmpp-mcp.exe` over real MCP stdio JSON-RPC (subset of `docker`)
- `llm` → real Claude session via Anthropic SDK (needs `ANTHROPIC_API_KEY`)
- `integration` → opt-in connect/disco test against a user-provided server

## Test fixture conventions

- Session-scoped `openfire` fixture handles compose up/down, the REST-API
  enable handshake (sets *5* DB-backed system properties — REST API, wildcard
  exclusions, external components on 5275), and pre-creates three
  persistent rooms (`r1`/`r2`/`r3`).
- Function-scoped `mcp` fixture builds a fresh `create_server()` per test
  and yields an in-process `fastmcp.Client` connected as `bot`.
- Function-scoped `raw_alice` / `raw_bob` / `raw_carol` fixtures connect
  raw slixmpp clients for the "other side" of conversations.
- Function-scoped `seclabel_component` fixture spins up a XEP-0258 stub on
  `seclabel.xmpp.test` (function-scoped on purpose — see gotcha #13).
- `temp_node` fixture in `test_pubsub_e2e.py` creates a uniquely-named node
  and cleans it up around each test.

## Known limitations / non-goals

- **Channel push requires the stdio transport.** Notifications ride the
  connection's standalone channel, which only exists after an `initialize`
  handshake; FastMCP's HTTP transport answers a modern client without one, so
  nothing is delivered (the server warns at startup). Claude Code spawns MCP
  servers over stdio, which is the supported path.
- **Channel security.** The lab runs plain-text c2s with a shared agent
  password and open in-band registration. Production needs real TLS,
  per-agent credentials, registration closed, and the relay behind
  `WEBHOOK_TOKEN` / `WEBHOOK_GITHUB_SECRET`. The permission-relay capability
  (`claude/channel/permission`) is deliberately not declared yet.
- **Relay delivery is at-most-once.** The queue survives an XMPP outage but
  not a relay restart; XEP-0198 stream management would close the gap.
- **Webhook authentication is per-sender, and only as strong as the scheme
  the sender offers.** GitHub signs the body (good, but the signature does
  not cover the destination and never expires — hence delivery-ID
  de-duplication and `WEBHOOK_ALLOWED_TARGETS`); GitLab and the generic
  fallback use a replayable shared token, so they want TLS in front. Source-IP
  restriction has to happen at the proxy, since the relay only sees the peer.
- **PEP-typed wrappers** (XEP-0163 `user_avatar` / `user_nick` / `user_tune`):
  not exposed. Plain pubsub at a user's bare JID works today (pass
  `service=<user-jid>`).
- **Pending-subscription approve/deny** (XEP-0060 §9.5): not exposed; only
  relevant for nodes with `access_model=authorize`.
- **XEP-0248 collection-node child/parent management**: collection nodes can
  be created (`pubsub#node_type=collection`), but explicit child-membership
  tools aren't exposed.
- **Publish preconditions / publish-options form** (XEP-0060 §7.1.5):
  not exposed.
- **XEP-0313 MAM (Message Archive Management)**: `mam_query` tool exists and
  the wiring is correct, but Openfire Monitoring 2.6.1 silently fails the
  requests (see gotcha #16). Live in-memory `search_messages` is the reliable
  path. Buffer size is `XMPP_INBOX_SIZE` (default 500, sliding window).
- **TLS hardening in the lab**: lab Openfire speaks STARTTLS with a
  self-signed cert and the test env sets `XMPP_TLS_INSECURE=true`. Production
  deployments should leave it `false`.
- **Single bot account per MCP server instance.** Multi-tenant is out of
  scope — run separate instances per identity if needed.
