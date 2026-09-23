# Per-host agent authentication for Prosody (optional)

This directory is **not part of xmpp-mcp proper**. xmpp-mcp — channel mode,
the agent tools, the webhook relay — works against any RFC 6120 server with
ordinary accounts (`XMPP_PASSWORD`). What lives here is one way of running a
fleet of agents without an account per agent: a deployment choice, kept
separate so the core can be taken (or upstreamed) without it.

The scheme has three pieces, which only make sense together:

| Piece | Where | What it does |
|---|---|---|
| `mod_auth_xmpp_mcp.lua` | here | Prosody auth provider: checks derived credentials against a master key |
| `xmpp-mcp-keys` | `src/xmpp_mcp/credentials.py` | Makes the master key and per-host keys |
| `XMPP_HOST_KEY_FILE` / `WEBHOOK_XMPP_HOST_KEY_FILE` | agent and relay | Mint a fresh password on every connect instead of using a stored one |

The Prosody lab (`python start-lab-prosody.py`,
`tests/integration/docker/prosody/`) is the scheme end to end, and
`tests/integration/test_prosody_auth_e2e.py` attacks it directly.

## How it works

The server holds one secret, the master key. Each agent host gets a key
derived from it, and each agent on that host derives its own password:

```
host_key = HMAC-SHA256(master,   "xmpp-mcp host v1|"  + host)
mac      = HMAC-SHA256(host_key, "xmpp-mcp agent v1|" + bare_jid + "|" + expiry)
password = "xmc1." + expiry + "." + base64url(mac)
```

Agents log in as `<session>.<host>@agents.example.com`. The module splits the
localpart at the first dot, re-derives the host key from the master, and
checks the MAC and the expiry. So:

* one host's key only mints that host's JIDs;
* a password is bound to its JID and expires (24 h by default; the server
  refuses more than 7 days);
* nothing is provisioned per agent — a new session simply logs in;
* a host is cut off by listing it in `xmpp_mcp_revoked_hosts`.

It is SASL PLAIN underneath (the server has to see the password to check
it), so TLS is required; Prosody enforces it with `c2s_require_encryption`
(its default). No SASL daemon or helper process is involved — the check runs
inside Prosody.

**Who can reach an agent.** The agents host has no account database. An
agent JID starts to exist — can receive messages, and have them stored while
it is offline — the first time it logs in, which takes that host's key.
Messages to a session ID that has never logged in bounce with
`service-unavailable`. With server-to-server disabled, the only senders are
authenticated users of this server: humans with accounts, and agents on hosts
holding a key (who can make up session IDs for themselves, but only on their
own host).

## Installing

For a ready-made server — Prosody in Docker with its own Let's Encrypt
certificates behind an existing web server — see [`deploy/`](deploy/README.md).
By hand:

1. Copy `mod_auth_xmpp_mcp.lua` into a directory on Prosody's `plugin_paths`.
   Prosody 13; it uses the `prosody.util.*` module names.
2. Make the master key where Prosody can read it and nothing else can:

   ```bash
   xmpp-mcp-keys new-master -o /etc/prosody/xmpp-mcp-master.key
   chown prosody: /etc/prosody/xmpp-mcp-master.key    # mode 0600 already
   ```

3. Put agents on their own virtual host (humans keep ordinary accounts on
   another):

   ```lua
   VirtualHost "agents.example.com"
       authentication = "xmpp_mcp"
       xmpp_mcp_master_key_file = "/etc/prosody/xmpp-mcp-master.key"
       -- xmpp_mcp_revoked_hosts = { "oldhost" }
       -- xmpp_mcp_max_ttl = 604800      -- longest lifetime accepted (s)
       -- xmpp_mcp_clock_skew = 300      -- grace after expiry (s)
   ```

4. For each agent host, make its key and ship it there (mode 0600, owned by
   the user the agents run as):

   ```bash
   xmpp-mcp-keys host-key --master /etc/prosody/xmpp-mcp-master.key --host host1 -o host1.key
   ```

5. Configure the agents and the relay on that host — see "Authentication:
   one secret per host" in `docs/CHANNELS.md`.

Refused logins are logged at `info` with the reason (not an agent JID, not a
derived credential, expired, lifetime too long, host revoked, bad
credential).

## Other servers

Only Prosody is implemented. Any server that can delegate password checks
(e.g. ejabberd's external auth) could run the same derivation; the reference
implementation is `verify_password` in `src/xmpp_mcp/credentials.py`.
