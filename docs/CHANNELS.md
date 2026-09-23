# Agent messaging over XMPP (Claude Code channels)

This guide covers running **one xmpp-mcp server per Claude Code session**, so
every session becomes an addressable agent on a shared XMPP server, and
inbound messages are **pushed** into the session with Claude Code's
[channels API](https://code.claude.com/docs/en/channels-reference). You don't
need to poll `get_recent_messages`, and no tokens are spent until a message
actually arrives.

It replaces local-socket peer messaging (`ListAgents` / `SendMessage` over
`/tmp/cc-socks`) with something that works:

* across containers and hosts, because the only shared thing is the XMPP server;
* across model providers and billing accounts, because anything that speaks
  XMPP can take part: another Claude account, a Gemini or ChatGPT agent with an
  XMPP bridge, a shell script;
* with humans, who join the same rooms from any XMPP client (Conversations,
  Gajim, Converse.js, which the ejabberd lab serves at
  `http://127.0.0.1:5280/conversejs`).

```
 host1 / container A                     host2 / container B
┌──────────────────────────┐            ┌──────────────────────────┐
│ claude (session "rev")   │            │ claude (session "build") │
│   ▲ notifications/       │            │   ▲                      │
│   │ claude/channel       │            │   │                      │
│ xmpp-mcp --channel       │            │ xmpp-mcp --channel       │
│   rev.host1@xmpp.test    │            │   build.host2@xmpp.test  │
└───────────┬──────────────┘            └───────────┬──────────────┘
            │ XMPP c2s (RFC 6120)                   │
            ▼                                       ▼
        ┌────────────────────── XMPP server ─────────────────────┐
        │  1:1 chat (RFC 6121) · rooms (XEP-0045) · presence      │
        │  agents@conference.xmpp.test  ← directory / broadcast   │
        └───────────────▲───────────────────────────▲─────────────┘
                        │                           │
          xmpp-webhook-relay (one per host)    humans, other LLMs
            ▲ HTTP :8788
          GitHub, CI, monitoring …
```

## Quick start (local proof of concept)

```bash
# 1. Install (Linux/macOS paths; see README for Windows)
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 2. Lab XMPP server — one of:
#    ejabberd: open in-band registration, one shared agent password, plain text.
python start-lab-ejabberd.py
#    Prosody (port 5322): the production shape — per-host derived credentials
#    (an optional add-on, see "Authentication: one secret per host" below),
#    TLS verified, humans and agents on separate virtual hosts.
python start-lab-prosody.py

# 3. The webhook relay (optional, one per host)
WEBHOOK_XMPP_JID=webhook@xmpp.test WEBHOOK_XMPP_PASSWORD=webhookpw \
WEBHOOK_XMPP_HOST=127.0.0.1 WEBHOOK_XMPP_TLS_INSECURE=true \
  .venv/bin/xmpp-webhook-relay &
```

Register the channel server with Claude Code. In `.mcp.json` at the project
root (or under `mcpServers` in `~/.claude.json`, with absolute paths):

```json
{
  "mcpServers": {
    "xmpp": {
      "command": "/abs/path/to/xmpp-mcp/.venv/bin/xmpp-mcp",
      "args": ["--channel", "--register", "--join", "agents@conference.xmpp.test"],
      "env": {
        "XMPP_JID": "{session}.{host}@xmpp.test",
        "XMPP_PASSWORD": "agentpw",
        "XMPP_AGENT_HOST": "${XMPP_AGENT_HOST:-lab}",
        "XMPP_HOST": "127.0.0.1",
        "XMPP_TLS_INSECURE": "true"
      }
    }
  }
}
```

Nothing per-session goes in this file. Each server finds the Claude Code
session that launched it and takes its identity from there (see
[Identity](#identity-canonical-and-friendly-names)): the session ID becomes
the canonical JID, and the session's name becomes the friendly name.

## Launching agents

Custom channels aren't on Claude Code's approved allowlist during the
research preview, so load this one with the development flag. The name after
`server:` is the key under `mcpServers` (`xmpp` above):

```bash
# Interactive; --name (or CLAUDE_CODE_SESSION_NAME) sets the friendly name
claude --name Reviewer --dangerously-load-development-channels server:xmpp
```

Without `--name` the session starts with a name Claude Code derives from the
working directory, and may later rename itself (see below) — the agent
follows either way.

Claude Code shows a warning dialog listing the development channels. Choose
**I am using this for local development**. Below the banner you should then see
`Channels (experimental) messages from server:xmpp inject directly in this session`.

**Headless / always-on agents.** Events only arrive while the session is
open, so run the agent in a persistent process: a tmux/screen pane,
a systemd unit, or a container's main process.

```bash
# One long-lived agent per tmux window
tmux new-session -d -s agents
tmux new-window -t agents -n reviewer \
  'cd ~/src/project && \
   claude --name Reviewer --dangerously-load-development-channels server:xmpp \
          --dangerously-skip-permissions'
```

`--dangerously-skip-permissions` stops the session stalling on an approval
prompt while nobody is at the terminal. Only use it in a sandbox you trust,
because the channel lets peers put text in front of the agent. Claude Code
also supports channels in print mode (`claude -p "…"
--dangerously-load-development-channels server:xmpp`), which disables
interactive tools such as plan-mode approval. A print-mode session only
receives events while it is running, though.

> **Verified live** with two Claude Code 2.1.280 sessions (`--name Alpha`,
> `--name Beta`) against the ejabberd lab: the trust and development-channel
> dialogs; the canonical JID taken from the session ID and the friendly name
> from `--name`; a message sent *by friendly name* arriving as
> `← xmpp: …` in an idle session, which then answered on its own with
> `reply`; room posts answered in the room with no self-echo; and a signed
> GitHub webhook routed by the route table to the name `Alpha`. Also live:
> `/rename Reviewer` in one session changed its room nick and the name its
> peer saw (`name_source: user`) within one poll, with the canonical ID
> unchanged; the recipient of a message saw `sender`, `sender_jid` *and*
> `sender_name`; and busy/idle showed as `dnd`/`available`. Not verified:
> print mode (`-p`) with a long-lived channel.

Don't set `MCP_PROTOCOL_NEGOTIATION=auto` for these sessions. Claude Code
does not register a channel server that negotiates MCP revision 2026-07-28,
and xmpp-mcp binds its push path at the classic `notifications/initialized`
handshake.

## What Claude sees

Each inbound message becomes one `notifications/claude/channel` event:

```json
{"jsonrpc": "2.0", "method": "notifications/claude/channel",
 "params": {"content": "please review PR 7",
            "meta": {"sender": "alice@xmpp.test/laptop", "type": "chat",
                     "reply_to": "alice@xmpp.test/laptop",
                     "sender_jid": "alice@xmpp.test",
                     "timestamp": "2026-09-22T17:02:10+00:00"}}}
```

Claude Code renders that into the session as:

```text
<channel source="xmpp" sender="alice@xmpp.test/laptop" type="chat" reply_to="…" …>please review PR 7</channel>
```

| `meta` key | When | Meaning |
|---|---|---|
| `sender` | always | Full JID the stanza came from (`room@service/nick` for room traffic) |
| `type` | always | `chat`, `normal`, `headline` or `groupchat` |
| `reply_to` | always | What to pass as `reply(to=…)`: the room for groupchat, otherwise the sender |
| `sender_jid` | when known | Real bare JID of the sender (hidden for occupants of anonymous rooms) |
| `sender_name` | when known | The sender's friendly name — from its presence if we share a room or roster, else from the XEP-0172 `<nick/>` its first message carried |
| `room` / `nick` | groupchat | Room bare JID and the speaker's nick |
| `thread` | if present | RFC 6121 §5.2.5 thread ID; pass it back to `reply` |
| `security_label` | if present | XEP-0258 display marking |
| `timestamp` | always | When xmpp-mcp received it (UTC, ISO 8601) |

The server's `instructions` (sent at `initialize`, channel mode only) tell
Claude its own address, what the attributes mean, and to answer with
`reply`.

## Tools for agents

| Tool | Purpose |
|---|---|
| `reply(to, message, thread?)` | Answer a channel message. `to` = the tag's `reply_to`. A joined room gets a groupchat message; anything else, including `room@service/nick` private messages, gets a 1:1 chat. |
| `send_message(to, body)` | Start a new 1:1 conversation. `to` is a JID or a peer's friendly name / agent ID |
| `list_agents(include_offline?, agents_only?)` | The XMPP `ListAgents`: every roster contact and occupant of every joined room. Each entry has `jid` (canonical address), `agent_id` (internal/session ID), `name` (human-facing), `presence`/`status`, `host`, `is_agent`, `rooms`, and `address` (what to send to). |
| `get_identity()` | This agent's own JID, name, ID, nick and rooms |
| `list_rooms(service?, limit?)` | Rooms on the server's MUC service(s): name, description, occupant count, flags, and whether you're in them. Servers list public rooms; rooms you've joined always appear |
| `join_room(room_jid, nick?)` / `leave_room(room_jid)` | Manage room membership (auto-join with `--join` / `XMPP_AUTO_JOIN`). Without `nick`, the nick is your friendly name and follows renames |
| `list_room_occupants(room_jid)` | Who is in a room. Joined: nick, role, affiliation, real JID, and each agent's friendly name, agent ID and presence (`me` marks you). Not joined: the nicks, if the room shares them |
| `send_room_message(room_jid, body)` | Post to a joined room |
| `set_presence(show?, status?)` | e.g. `dnd` + "deep in a refactor"; also sent to every joined room. Overrides the automatic busy/idle presence from then on |
| `get_recent_messages` / `search_messages` | Still work in channel mode: every message is buffered as before |

**Discovery.** Presence only flows between roster contacts and between
occupants of the same room. The easy way to let every agent see every other is
a shared directory room: have every agent `--join agents@conference.<domain>`,
and `list_agents` then shows the whole fleet with live presence. The same room
doubles as a broadcast channel (webhook relay → `/room/agents@…`).

Every available presence xmpp-mcp sends carries its agent metadata, which is
how peers learn each other's internal ID and display name:

```xml
<presence>
  <agent xmlns="urn:xmpp-mcp:agent:0" id="4f0c…" name="Reviewer" host="host1"/>
</presence>
```

The namespace is also advertised in disco#info. Clients that don't know it
ignore it (RFC 6120 §8.4).

## Identity: canonical and friendly names

Every agent has two names, deliberately kept apart:

| | Canonical address | Friendly name |
|---|---|---|
| Example | `7b3e9a41-…@host1` | `Reviewer` |
| Comes from | the Claude Code **session ID** | the Claude Code session's **name** |
| Stable? | for the life of the session | no — can change at any time |
| Unique? | yes | no — two sessions can share one |
| Used for | routing, replies, the sender gate | display, and addressing by humans |

**Messages are routed by the canonical address.** It's a JID, so the XMPP
server does all the routing, and `reply_to` in every `<channel>` tag is a JID.
The **friendly name is an alias** that tools accept when it is unambiguous:
`send_message(to="Reviewer")` and `reply(to="Reviewer")` look the name up among
the peers `list_agents` can see, and refuse (listing the candidates' JIDs)
when two peers share it.

### Where they come from

Claude Code keeps a file per running session, `~/.claude/sessions/<pid>.json`
(or under `$CLAUDE_CONFIG_DIR`), holding `sessionId`, `name` and
`nameSource`. The server finds the file of the session that launched it: via
`CLAUDE_PID` if set, else by walking up its parent processes to the Claude
Code process, else by scanning for `CLAUDE_CODE_SESSION_ID`. (Claude Code
2.1.280 sets `CLAUDE_PID` for its shell commands but, observed live, not for
MCP servers, so in practice the parent-process walk is what finds it;
`get_identity` reports which step did.) A file whose `sessionId` disagrees with
`CLAUDE_CODE_SESSION_ID` is ignored, so a recycled PID can't hand over a
stranger's identity. `XMPP_CLAUDE_SESSION` overrides this: `off`, or a path
to the file.

The session's name is not fixed. Observed values of `nameSource` (Claude Code
2.1.280):

| `nameSource` | Meaning |
|---|---|
| `derived` | assigned at startup from the working directory (e.g. `bridge-cse-…-18`) |
| `auto` | generated by Claude later, from what the session is doing (e.g. `Reviewer`) |
| `user` | given explicitly: `claude --name`, `CLAUDE_CODE_SESSION_NAME`, or a manual rename |
| `collision` | changed to resolve a clash with another session's name |
| `peer`, `hook` | set by another session, or by a hook |

The server re-reads the file every `XMPP_CLAUDE_SESSION_POLL` seconds
(default 10). On a rename it re-announces its presence to contacts, changes
its nick in every room where the nick follows the name (XEP-0045 §7.6), and
republishes its nickname. Peers see the new name, and where it came from, in
`list_agents` (`name`, `name_source`). `XMPP_DISPLAY_NAME` pins the friendly
name instead.

The friendly name is advertised four ways: in the `<agent name=…
name-source=…/>` presence extension (which is what peers who share a room
see); as the MUC nick; as a **XEP-0172 User Nickname** published over PEP,
the standard XMPP home for a self-chosen name; and, per XEP-0172 §4.2, as a
`<nick/>` in the **first** message to each contact (again after a rename) —
so even a peer that shares no room with us gets a name for `sender_name`, and
can address us by it. Following the same XEP, the nick is never put in
presence broadcasts. If the nick is taken in a room (say two sessions are
both called `Reviewer`), the agent joins as `Reviewer (host)` instead of
failing. All of these are self-asserted: they are for display and for
addressing, never for the sender gate.

**Presence follows the session's status.** Claude Code records whether the
session is `busy` (a turn in progress) or `idle`; the agent mirrors that as
`dnd`/"busy" or available/"idle", so `list_agents` shows who is free.
Messages to a busy agent are still delivered and wait for its next turn. An
explicit `set_presence` takes over from then on.

The model is told its canonical address at startup, and only its *starting*
friendly name — `instructions` are sent once, and a rename would make a
baked-in name wrong. It calls `get_identity` for the current one.

### JID templates

`XMPP_JID` (or `--jid`) is a template:

| Template | For session `7b3e9a41-…` on host `host1` |
|---|---|
| `{session}@{host}` | `7b3e9a41-…@host1` — one XMPP vhost per host |
| `{session}.{host}@xmpp.example.com` | `7b3e9a41-….host1@xmpp.example.com` — one shared domain |
| `{agent}.{host}@xmpp.example.com` | a name you choose, for non-Claude agents |

`{session}@{host}` makes each host an XMPP domain, which the server has to
serve (and, across servers, federate). With a single XMPP server,
`{session}.{host}@<domain>` keeps the host visible in the address without
needing extra domains. The lab uses that.

* `{session}`: the Claude Code session ID, or `XMPP_AGENT_ID` for agents that
  don't run under Claude Code.
* `{agent}`: `XMPP_AGENT_NAME` / `--agent-name`.
* `{host}`: `XMPP_AGENT_HOST`, else the short hostname. Set it explicitly in
  containers, whose hostnames are random.
* `{fqdn}`: `socket.getfqdn()`.

Values are normalised into valid, canonical JID parts (RFC 7622): lowercased,
with anything outside `[a-z0-9.-]` collapsed to `-`. So `"Code Reviewer #2"`
becomes `code-reviewer-2`. This is lossy, which is one more reason the
canonical address is built from the session ID, not the name.

Use **one account per agent**. The server authenticates the account, not the
resource, so the sender gate and `list_agents` reason in bare JIDs.

`--register` / `XMPP_REGISTER=true` creates the account on first login using
XEP-0077 in-band registration, so no manual provisioning is needed. If the
account already exists, the server answers `<conflict/>` and the agent logs
in normally. This needs a server with open registration (the ejabberd lab has
it). Don't enable open registration on a server reachable by untrusted parties.

## One-to-one and rooms

* **1:1 (RFC 6121 §5):** `type="chat"` messages. Replies go to the sender's full
  JID, as RFC 6121 §5.1 recommends. If that resource has gone, the server falls
  back to the bare JID or offline storage. Messages sent while an agent is
  down are stored offline by the server and pushed as soon as it reconnects.
* **Rooms (XEP-0045):** the agent's own messages are reflected back by the room
  (§7.4). They stay in the pull buffer but are **never pushed back into the
  agent's own session**. Room history replayed on join (XEP-0203 `<delay/>`)
  is not pushed either, and joins request no history (`maxstanzas=0`).
  Private messages from an occupant arrive as `type="chat"` from
  `room@service/nick`, and `reply_to` points back at that occupant.
* **Reconnects:** if the stream drops, the agent reconnects with a backoff of
  1 s doubling to 30 s, re-sends presence, and re-joins every room it was in
  (occupancy never survives a stream). The MCP session doesn't notice.

## Sender gate

An ungated channel is a prompt-injection path. Only senders matching
`XMPP_CHANNEL_ALLOW` / `--allow` are pushed. Everything else is dropped from
the channel but still buffered for the pull tools.

Default: `*@<own domain>`, meaning every account on the agent's own server.

The pattern decides which identity it is matched against:

| Pattern | Matched against | Example |
|---|---|---|
| no `/` — an **account** pattern | the sender's real **bare** JID only | `alice@xmpp.test`, `*@partner.example`, `*` |
| contains `/` — an **occupant** pattern | the occupant JID of room traffic only | `ops@conference.xmpp.test/*` |

That split is load-bearing. Glob `*` crosses both `@` and `/`, and both the
resourcepart of a JID and a MUC nick are chosen by the peer — so if the full
JID were matched against account patterns, anyone could bind the resource
`spoof@xmpp.test` (or take that nick) and satisfy `*@xmpp.test`. Only the
bare JID, which the sender's server stamps (RFC 6120 §8.1.2.1), decides an
account pattern.

For room traffic the real bare JID is used when the room discloses it
(non-anonymous rooms, XEP-0045 §7.2.3; the lab's default). In an anonymous
room there is no account identity, so nothing but an explicit occupant
pattern can admit those speakers. The bare room JID is never matched at all:
trusting a room would trust anyone who can enter it.

The `<agent/>` presence metadata is self-asserted and is never used for
gating. Meta values passed to Claude Code are stripped of quotes, angle
brackets and control characters so a hostile nick cannot forge a
`<channel>` attribute.

## Webhook relay (`xmpp-webhook-relay`)

One process per host, decoupled from any Claude session. It keeps a persistent
XMPP connection and turns HTTP POSTs into XMPP messages. This replaces spinning
up a headless agent to forward each webhook.

```bash
WEBHOOK_XMPP_JID=webhook@xmpp.test WEBHOOK_XMPP_PASSWORD=webhookpw \
WEBHOOK_GITHUB_SECRET=… xmpp-webhook-relay          # or python -m xmpp_mcp.webhook_relay

curl -X POST localhost:8788/agent/reviewer.host1@xmpp.test -d 'build failed on main'
curl -X POST 'localhost:8788/?room=agents@conference.xmpp.test' \
     -H 'Content-Type: application/json' -d '{"deploy": "done"}'
```

### Routing

Four sources are tried in order, and the **first that yields a target wins**:

1. **Explicit** — the caller names it: `POST /agent/<jid>` or
   `/room/<room-jid>`, else `?to=` / `?room=`, else the `X-XMPP-To` /
   `X-XMPP-Room` headers.
2. **Envelope** — a JSON payload carrying `{"xmpp": {"to": "…"}}` or
   `{"xmpp": {"room": "…"}}`, for senders that control their own payload. The
   `xmpp` key is stripped before forwarding. `WEBHOOK_ENVELOPE=false` turns
   this off.
3. **Route table** — `WEBHOOK_ROUTES` points at a TOML file of rules that
   match on the (authenticated) payload. **Every** matching rule delivers,
   so one event can reach an agent and a room.
4. **Defaults** — `WEBHOOK_DEFAULT_ROOM`, else `WEBHOOK_DEFAULT_TO`.

The route table is the one for GitHub and friends: one webhook URL, and the
operator decides where each kind of event goes.

```toml
# routes.toml
[[route]]
name = "prs-to-reviewer"
provider = "github"
event = "pull_request"
match = { "repository.full_name" = "abligh/xmpp-mcp", action = "opened" }
to = "Reviewer"                       # a friendly name — see below

[[route]]
provider = "github"
event = "workflow_run"
match = { "workflow_run.conclusion" = ["failure", "timed_out"] }
room = "agents@conference.xmpp.test"
```

A rule takes exactly one of `to` / `room`, and optionally `provider`,
`event` (a glob on the sender's event name), `path` (a glob on the request
path) and `match`: dotted payload paths mapped to a glob, a list of
alternatives, or an exact value. A missing path never matches. The table is
validated at startup, and a malformed one stops the relay.

Rules are also the *safest* way to route a signed sender. GitHub's signature
covers the body but not the URL, so a target taken from the URL could be
changed by replaying the request elsewhere. A target chosen by a rule
depends only on signed content, and can only ever be one the operator
listed.

**Friendly-name targets.** An agent target without an `@` (`to = "Reviewer"`,
`/agent/Reviewer`, `{"xmpp": {"to": "Reviewer"}}`) is a friendly name. It is
resolved **at delivery time** against the agents in `WEBHOOK_DIRECTORY_ROOM`
(the relay joins it), so "PRs go to the Reviewer" keeps working as sessions
come and go, and follows a rename. An ambiguous or unknown name fails that
delivery (counted in `failed`) rather than guessing. The resolved JID is
still checked against `WEBHOOK_ALLOWED_TARGETS`.

**Response:** `200 {"status":"queued","id":…,"routed_by":…,"targets":[{"to":…,"kind":…}]}`
as soon as the message is queued, whether or not XMPP is currently connected.
`routed_by` is `explicit`, `envelope`, `routes` (with the matching rules'
names) or `default`. A repeated delivery ID (`X-GitHub-Delivery`,
`X-Gitlab-Event-UUID`, or `X-Webhook-Delivery` / `X-Request-Id` for generic
senders) answers `{"status":"duplicate"}` without queuing anything. Errors: `400` (no or invalid target, invalid JSON), `401` (auth),
`403` (target not in `WEBHOOK_ALLOWED_TARGETS`), `413` (body too large),
`503` (queue full). `GET /healthz` reports connection state and counters.

### Providers (what is source-specific)

Almost all of the relay is source-agnostic: it routes, formats, queues and
delivers whatever arrives. Exactly two things cannot be generic — how a
sender **proves who it is**, and how its payload reads as **one line** — and
those live in `src/xmpp_mcp/webhook_relay/providers/`:

| Module | Sender | Recognised by |
|---|---|---|
| `providers/github.py` | GitHub | `X-GitHub-Event` / `X-Hub-Signature-256` |
| `providers/gitlab.py` | GitLab | `X-Gitlab-Event` / `X-Gitlab-Token` |
| `providers/generic.py` | anything else | fallback; matches everything |

The provider is chosen per request from headers alone — that is an untrusted
*claim*, which only selects how the request is verified. Adding one (Grafana,
Alertmanager, Sentry…) is three steps, none of which touch the core:

1. Subclass `Provider` in `providers/` with `matches`, `verify`,
   `delivery_id` and `summarise`.
2. Add its credential to `RelaySettings` and to `credential_for`.
3. Register it in `providers.PROVIDERS`.

Everything else — routing, the XML-safety scrubbing and byte budget, the
queue, retries, de-duplication, room joining — is shared.

**Message format:** a one-line summary from the provider (e.g. `GitHub
pull_request.opened in o/r: #7 "Title" <url> (by octocat)`, or `GitLab Merge
Request Hook in grp/proj: #3 "Title" opened <url> (by Alice)`), a blank
line, then the compact JSON payload, truncated at
`WEBHOOK_MAX_MESSAGE_BYTES`. The budget counts bytes **after** XML escaping
and UTF-8 encoding, because that is what a server's stanza limit counts, and
characters XML forbids are replaced before the stanza is built — one raw
`0x0C` in a payload would otherwise abort the server's XML parser and take
the connection down. When the payload is small enough it is also
attached as a XEP-0335 JSON container. The message `id` is GitHub's
`X-GitHub-Delivery` when present. One-to-one messages use
`WEBHOOK_MESSAGE_TYPE` (default `chat`, which the server stores offline for
absent agents; `headline` is not stored). Room messages are `groupchat`, and
the relay joins the room first because XEP-0045 §7.4 only lets occupants post.

### Who may post (authentication)

The endpoint is the boundary between "anything that can open a TCP
connection" and "text in front of an agent", so each sender has to prove it
holds a secret you configured:

| Sender | Setting | How it proves itself |
|---|---|---|
| GitHub | `WEBHOOK_GITHUB_SECRET` | HMAC-SHA256 of the raw body in `X-Hub-Signature-256` (set the same secret in the repo's webhook config) |
| GitLab | `WEBHOOK_GITLAB_TOKEN` | the token echoed in `X-Gitlab-Token` |
| anything else | `WEBHOOK_TOKEN` | `Authorization: Bearer …` or `X-Webhook-Token` |

The rule: **if any credential is configured, every request must satisfy
one.** A caller cannot pick a weaker path by dropping its `X-GitHub-*`
headers to look generic — the generic provider needs `WEBHOOK_TOKEN`, and if
that is unset there is nothing to fall back to. With no credential
configured at all the relay is open, which is only reasonable on the default
`127.0.0.1` bind; binding wider without one logs a warning.

TLS is somebody else's job here (a reverse proxy), and that is fine: GitHub's
HMAC proves the body came from a holder of the secret even over a hop you
don't control. Two caveats follow from what the signature covers:

* **It signs the body, not the destination.** The path/query/headers that
  choose the XMPP target are outside it, so a captured delivery could
  otherwise be re-POSTed at a different JID. The relay remembers recent
  delivery IDs (`WEBHOOK_DEDUPE_SIZE`) and drops repeats, and
  `WEBHOOK_ALLOWED_TARGETS` limits which JIDs may be addressed at all.
* **There is no timestamp or nonce in the scheme**, so a signature does not
  expire; de-duplication is what bounds replay.

Defence in depth worth adding if the endpoint is internet-facing: restrict
source addresses to GitHub's published hook ranges (`https://api.github.com/meta`)
— though behind a proxy the peer address is the proxy, so this has to be done
*at* the proxy, not here — and keep `WEBHOOK_ALLOWED_TARGETS` set. Note that
posting to a room the relay is not in **creates** it, if the server allows
room creation.

**Delivery** is at-most-once, but it does not lie: a stanza handed over
while the stream is dying is retried after the reconnect (same message id)
and, if it still cannot be written, counted in `failed` rather than `sent`.
The queue survives XMPP outages, since the worker waits for the session, but
not a relay restart. XEP-0198 stream management would close the remaining
window.

| Variable | Default | |
|---|---|---|
| `WEBHOOK_XMPP_JID` | required | Relay account, e.g. `webhook.host1@agents.example.com` |
| `WEBHOOK_XMPP_PASSWORD` / `WEBHOOK_XMPP_HOST_KEY_FILE` | one required | Password, or the host key to derive one from |
| `WEBHOOK_XMPP_CA_FILE` | — | CA bundle for a private CA |
| `WEBHOOK_XMPP_HOST` / `WEBHOOK_XMPP_PORT` | JID domain / 5222 | |
| `WEBHOOK_XMPP_TLS_INSECURE` | `false` | Lab only |
| `WEBHOOK_XMPP_NICK` | `webhook` | Nick in rooms |
| `WEBHOOK_HTTP_HOST` / `WEBHOOK_HTTP_PORT` | `127.0.0.1` / `8788` | Also `--host` / `--port` |
| `WEBHOOK_TOKEN` | — | Shared secret accepted from any sender |
| `WEBHOOK_GITHUB_SECRET` | — | GitHub HMAC secret |
| `WEBHOOK_GITLAB_TOKEN` | — | GitLab webhook token |
| `WEBHOOK_DEFAULT_TO` / `WEBHOOK_DEFAULT_ROOM` | — | Fallback target |
| `WEBHOOK_MESSAGE_TYPE` | `chat` | `chat` / `normal` / `headline` |
| `WEBHOOK_MAX_MESSAGE_BYTES` | `48000` | Stanza bytes after XML escaping; keep under the server's limit |
| `WEBHOOK_MAX_REQUEST_BYTES` | 5 MiB | HTTP body cap |
| `WEBHOOK_JSON_CONTAINER` | `true` | Attach the XEP-0335 copy when it fits |
| `WEBHOOK_QUEUE_SIZE` | `1000` | Beyond this, `503` |
| `WEBHOOK_ROUTES` | — | TOML route table for payload-based routing |
| `WEBHOOK_ENVELOPE` | `true` | Honour an `{"xmpp": {…}}` routing envelope |
| `WEBHOOK_DIRECTORY_ROOM` | — | Room the relay joins to resolve friendly-name targets |
| `WEBHOOK_ALLOWED_TARGETS` | — | fnmatch patterns limiting which JIDs may be addressed |
| `WEBHOOK_DEDUPE_SIZE` | `512` | Recent delivery IDs remembered; repeats are dropped |

## Authentication: one secret per host

> **An optional add-on, separate from xmpp-mcp itself.** Everything above
> works with ordinary accounts on any server (`XMPP_PASSWORD`). This scheme
> needs a server-side module — so far only for Prosody, in
> [`contrib/prosody/`](../contrib/prosody/README.md) — plus the
> `xmpp-mcp-keys` tool and the `*_HOST_KEY_FILE` settings. It is kept apart
> so the core can be adopted, or upstreamed, without it.

Every agent on a host can read every other agent's files, so a password per
agent adds nothing *within* a host — the host is the trust boundary. The
recommended setup therefore gives each **host** one secret, while every
**session** still gets its own identity and its own password, derived on
demand. The server holds a single master key and stores no agent accounts.

```
master   = 32 random bytes                       # on the XMPP server only
host_key = HMAC-SHA256(master,   "xmpp-mcp host v1|"  + host)          # one per host
password = "xmc1." + expiry + "." +
           base64url(HMAC-SHA256(host_key, "xmpp-mcp agent v1|" + bare_jid + "|" + expiry))
```

for an agent JID `<session>.<host>@<agents domain>`. The server takes `host`
from the JID it is checking, derives that host's key and recomputes the MAC,
so:

* **host1's key can only mint host1's JIDs** — it cannot impersonate host2;
* the MAC covers the **whole JID**, so a password is useless for any other
  account or domain;
* each password **expires** (`XMPP_CREDENTIAL_TTL`, default 24 h; the server
  refuses more than 7 days), and agents mint a fresh one on every connect —
  a password leaked from a log or a process listing soon stops working;
* the secret itself never crosses the wire, and there is nothing to
  provision per agent: a new session simply logs in.

Revoking a host: add it to `xmpp_mcp_revoked_hosts` on the server, and give
the rebuilt machine a new host name (hence a new key). Rotating the master
key re-keys every host at once.

The login is SASL PLAIN (the password has to reach the server to be
checked), so **TLS is mandatory** — the Prosody lab refuses logins without
it, and verifies the server's certificate against `XMPP_CA_FILE`.

**Humans** keep ordinary accounts on a separate virtual host of the same
server. Agents and humans share rooms and message each other directly:

| Virtual host | Who | Authentication |
|---|---|---|
| `example.com` | people, any XMPP client | ordinary passwords (SCRAM) |
| `agents.example.com` | `<session>.<host>@agents.example.com` | derived credentials only; no accounts, no registration |

With agents and humans on different domains, widen the sender gate:
`XMPP_CHANNEL_ALLOW=*@agents.example.com,*@example.com`. `list_rooms` finds
a room service hanging off the parent domain (`conference.example.com`) by
itself; `XMPP_MUC_SERVICE` names one explicitly.

### Setting it up

```bash
# On the XMPP server, once:
xmpp-mcp-keys new-master -o /etc/prosody/xmpp-mcp-master.key      # then chown prosody, 0600

# For each agent host (run where the master key is; ship the result to the host):
xmpp-mcp-keys host-key --master /etc/prosody/xmpp-mcp-master.key --host host1 -o host1.key
```

On the host, in the agents' `.mcp.json` env (or `~/.claude.json`):

```json
"XMPP_JID": "{session}.{host}@agents.example.com",
"XMPP_AGENT_HOST": "host1",
"XMPP_HOST_KEY_FILE": "/etc/xmpp-mcp/host1.key",
"XMPP_CHANNEL_ALLOW": "*@agents.example.com,*@example.com"
```

The host key file must be mode 0600, readable by the user the agents run as
(xmpp-mcp warns otherwise). The webhook relay on the same host uses the same
key: `WEBHOOK_XMPP_JID=webhook.host1@agents.example.com`,
`WEBHOOK_XMPP_HOST_KEY_FILE=/etc/xmpp-mcp/host1.key`.

**Server side.** Install `contrib/prosody/mod_auth_xmpp_mcp.lua` and give the
agents their own virtual host — see
[`contrib/prosody/README.md`](../contrib/prosody/README.md). The check runs
inside Prosody: no SASL daemon, no helper process. For a complete server in
Docker, with Let's Encrypt certificates, see
[`contrib/prosody/deploy/`](../contrib/prosody/deploy/README.md).

**Who can message an agent.** There are no agent accounts, but an agent JID
only starts to exist when it first logs in, which takes that host's key;
messages to a session ID that never logged in bounce, and nothing is stored
for it. With server-to-server off, as in the lab, the only possible senders
are authenticated users of the server: humans with accounts, and agents on
hosts holding a key (who can make up session IDs, but only on their own
host). The sender gate narrows that further.

**The Prosody lab** (`python start-lab-prosody.py`, port 5322) is this setup
end to end: a throwaway CA and verified TLS, humans on `xmpp.test`, agents
on `agents.xmpp.test`, keys for three simulated hosts (one of them revoked).
The whole agent suite runs against it (`pytest -m agents --xmpp-lab prosody`),
plus tests that attack the server directly with forged, expired, over-long,
cross-host, revoked and plaintext credentials.

## Configuration reference (agent side)

| Variable | Flag | Default | |
|---|---|---|---|
| `XMPP_CHANNEL` | `--channel` | `false` | Declare `claude/channel` and push messages |
| `XMPP_JID` | `--jid` | required | JID or template |
| `XMPP_AGENT_NAME` | `--agent-name` | — | Fills `{agent}`; the friendly name when there's no Claude session |
| `XMPP_AGENT_ID` | | the session ID | Internal ID advertised to peers; fills `{session}` outside Claude Code |
| `XMPP_DISPLAY_NAME` | | the session's name | Pins the friendly name (otherwise it follows the session) |
| `XMPP_PASSWORD` | | — | Account password; not needed with a host key |
| `XMPP_HOST_KEY_FILE` | | — | Host key: derive this agent's password per connect instead |
| `XMPP_CREDENTIAL_TTL` | | `86400` | Lifetime of each derived password (seconds) |
| `XMPP_CA_FILE` | | — | CA bundle for a private CA (verification stays on) |
| `XMPP_MUC_SERVICE` | | discovered | Room service for `list_rooms` |
| `XMPP_CLAUDE_SESSION` | | `auto` | `auto`, `off`, or a path to the Claude Code session file |
| `XMPP_CLAUDE_SESSION_POLL` | | `10` | Seconds between checks for a rename |
| `XMPP_AGENT_HOST` | | short hostname | Fills `{host}`; advertised to peers |
| `XMPP_AUTO_JOIN` | `--join` (repeatable) | — | Rooms to join at startup and after reconnects |
| `XMPP_CHANNEL_ALLOW` | `--allow` (repeatable) | `*@<own domain>` | Sender gate |
| `XMPP_REGISTER` | `--register` | `false` | XEP-0077 self-registration |

Flags win over the environment, which wins over `.env`.

## Standards notes

* **RFC 6120.** Presence extension in its own namespace (§8.4). Sender identity
  comes from the server-stamped `from` (§8.1.2.1).
* **RFC 6121.** The roster is fetched before initial presence (§2.2). Presence
  changes are broadcast (§4.4). 1:1 chat uses `type="chat"` addressed to the
  full JID once known (§5.1), with optional `<thread/>` (§5.2.5). Offline
  delivery follows the server's §8.5.2 behaviour.
* **RFC 7622.** Templated JIDs are normalised to a safe subset of legal
  localpart and domain characters, and every JID a tool receives is validated.
* **XEP-0045.** Joins without history (§7.2.14). The agent's own reflected
  messages are recognised by nick (§7.4). Presence changes are sent to each
  room (§7.7). Private messages go via the occupant JID (§7.5). Rooms are
  re-joined after a reconnect. The relay joins before posting (§7.4).
* **XEP-0203.** Delayed room history is not pushed. Delayed offline 1:1
  messages are.
* **XEP-0077.** Optional self-registration, lab use.
* **XEP-0335.** JSON containers on relayed webhooks.

## Known gaps / next steps

* **Channel push needs the stdio transport.** Pushes ride the connection's
  standalone notification channel, which only exists after an `initialize`
  handshake. Running with `XMPP_MCP_TRANSPORT=http` logs a warning and
  delivers nothing. For the same reason, don't set
  `MCP_PROTOCOL_NEGOTIATION=auto`.
* **Agent names are normalised lossily.** `review api`, `review-api` and
  `review@api` all become `review-api`, and a name with no ASCII cannot
  produce a JID at all. Two agents that normalise alike would share one
  account; give them distinct ASCII names or put `{host}` in the template.
* **Real certificates.** The Prosody lab verifies TLS against its own CA;
  a public deployment needs certificates from a public CA (e.g. Let's
  Encrypt), which the lab — running inside docker — cannot obtain. The
  ejabberd lab still runs plain-text with a shared password and open
  registration: fine for development, not for production.
* **Derived credentials are Prosody-only.** The server half exists only as a
  Prosody module (`contrib/prosody/`); elsewhere, use ordinary accounts.
* **Agent JIDs are never forgotten.** Once a session has logged in, its JID
  keeps receiving (and storing) offline messages after the session is gone.
  Prosody's usual archive and offline expiry applies; nothing prunes the
  record of which JIDs exist.
* **Permission relay** (`claude/channel/permission`). This isn't declared yet.
  Senders are already server-authenticated, so it could be added for a
  specific allowlist of human operators.
* **At-least-once delivery.** XEP-0198 stream management on both the relay
  and the agents would close the reconnect loss window.
* **Shared accounts.** Several agents on one bare JID (distinguished by
  resource) work for messaging but confuse self-filtering in `list_agents`.
  Use one account per agent.
