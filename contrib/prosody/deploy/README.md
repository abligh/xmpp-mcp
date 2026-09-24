# Deploying Prosody for people and xmpp-mcp agents

A single container: Prosody 13 with the agent auth module
(`../mod_auth_xmpp_mcp.lua`), plus certbot for its own Let's Encrypt
certificates. It is written for a host whose web server (Caddy or Apache)
already owns port 80. Every site-specific value lives in `.env`; the names
below are the placeholders from `.env.example`.

| Name | What for | Certificate |
|---|---|---|
| `jabber.example.com` | people: ordinary accounts, any XMPP client | Let's Encrypt |
| `jabber-agent.example.com` | agents: `<session>.<host>@…`, derived credentials | Let's Encrypt |
| `conference.jabber.example.com` | rooms, shared by both | none needed |

Both client domains need their own certificate because an XMPP client checks
the certificate against the domain of the JID it logs in as. The room service
needs no DNS record and no certificate: clients reach it through the server,
and no other server may talk to it.

## What is exposed

| Port | Where | Why |
|---|---|---|
| 5222/tcp | all interfaces | XMPP clients; STARTTLS is required, then SASL |
| 5269/tcp | all interfaces | server-to-server, only with the push services in `S2S_ALLOWED_DOMAINS` |
| 8480/tcp | 127.0.0.1 only | certbot's HTTP-01 answers, reached through the host's web server |

Port 80 stays with the host's web server, which forwards
`/.well-known/acme-challenge/` for the two names to 127.0.0.1:8480 and refuses
everything else. **Port 443 isn't involved**: these names aren't websites, and
XMPP clients connect on 5222. (Docker publishes 5222 and 5269 past `ufw`; an
external firewall has to let 5222, 5269 and 80 in.)

## Agents in everyone's contact list

`mod_agent_roster` (see `../README.md`) puts every agent in the Everyone room
(`EVERYONE_ROOM`, default `everyone@<MUC_DOMAIN>`) into every person's
contact list, named by the agent's current name, and every person into every
agent's. Renames show up in clients as they happen. Agents join the room by
configuration: add it to `XMPP_AUTO_JOIN` in the agents' settings.
`EVERYONE_ROOM=off` turns this off.

## Phone push (XEP-0357)

A phone app that is closed can't hold a connection, so to deliver a message
the server has to wake it: it sends a notification to the app vendor's push
service, which passes it to Apple or Google. The notification carries no
message text and no sender; the woken app fetches the message itself.

That hop is server-to-server XMPP, so `S2S_ALLOWED_DOMAINS` lists the push
services this server may talk to — for Monal `eu.prod.push.monal-im.org`, for
Conversations `p2.conversations.im`. `mod_s2s_whitelist` refuses every other
domain in both directions: messages to other servers bounce with
`not-allowed`, and a server claiming to be anything else is disconnected
before it can authenticate. With the setting empty, server-to-server isn't
loaded at all. Changes apply with `docker compose up -d`.

To check push is working, send yourself a message while the app is closed,
then look for `Push notifications enabled` and any s2s errors in
`docker compose logs`.

## Setting it up

1. **The web server.** Forward the challenge path. Two versions, same effect:

   * Caddy — `caddy/jabber-acme.Caddyfile`: add it to the Caddyfile (or
     `import` it), then
     `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`.
     The `http://` prefix is essential: without it Caddy claims the names
     itself and redirects Let's Encrypt to HTTPS, away from the challenge.

   * Apache — `apache/jabber-acme.conf`:

     ```bash
     sudo cp apache/jabber-acme.conf /etc/apache2/sites-available/
     sudo a2enmod proxy proxy_http
     sudo a2ensite jabber-acme
     sudo apachectl configtest && sudo systemctl reload apache2
     ```

   Whichever server actually answers port 80 is the one that needs this
   (`ss -ltnp 'sport = :80'` shows which). Check: `curl -sI http://jabber.example.com/.well-known/acme-challenge/x`
   should give **502 or 503** (forwarded, nothing listening yet), not a
   redirect or 404.

2. **Configure.** In this directory:

   ```bash
   cp .env.example .env
   ```

   Set the three domains and `ACME_EMAIL`.

   The directory can be copied out of the repository, e.g. to keep it with
   the host's other Docker configuration. It is self-contained except for the
   auth module, which the build takes from `XMPP_MCP_MODULE_DIR` (default
   `..`, right inside the repository). Outside, either copy
   `mod_auth_xmpp_mcp.lua` alongside and set `XMPP_MCP_MODULE_DIR=.`, or
   point it into an xmpp-mcp checkout, so a `git pull` there picks up module
   changes on the next `docker compose build`. Leave `ACME_STAGING=1` for the first start.

3. **Start.**

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

   On first start the container creates the master key in `./secrets/`,
   obtains a certificate for each name, and only then starts Prosody. If a
   challenge fails it says so and retries every 15 minutes. It doesn't retry
   faster, because Let's Encrypt allows only a few failures per hour.

4. **Switch to real certificates.** When staging worked, set
   `ACME_STAGING=0` in `.env` and `docker compose up -d`. The container sees
   the certificates came from another CA and replaces them at once.

5. **People.** Create accounts (there is no self-registration):

   ```bash
   docker compose exec prosody prosodyctl adduser alice@jabber.example.com
   ```

6. **Agent hosts.** One key per host, made from the master key inside the
   container:

   ```bash
   (umask 077; docker compose exec -T prosody xmpp-mcp-host-key host1 > host1.key)
   ```

   Copy `host1.key` to host1, mode 0600, readable by the user the agents run
   as. On host1, in the agents' `.mcp.json` env (or `~/.claude.json`):

   ```json
   "XMPP_JID": "{session}.{host}@jabber-agent.example.com",
   "XMPP_AGENT_HOST": "host1",
   "XMPP_HOST_KEY_FILE": "/etc/xmpp-mcp/host1.key",
   "XMPP_CHANNEL_ALLOW": "*@jabber-agent.example.com,*@jabber.example.com"
   ```

   No `XMPP_HOST`, `XMPP_PORT` or `XMPP_CA_FILE` is needed: the domain
   resolves to the server, 5222 is the default port, and Let's Encrypt is publicly
   trusted. Rooms are at `…@conference.jabber.example.com`; `list_rooms`
   finds that service without `XMPP_MUC_SERVICE`. A webhook relay on host1
   uses the same key, as `webhook.host1@jabber-agent.example.com`.

## Running it

* **Renewal** runs inside the container twice a day. certbot renews within 30
  days of expiry, and Prosody reloads the new certificates in place, so
  connections aren't dropped.
* **Revoking a host**: add it to `XMPP_MCP_REVOKED_HOSTS` in `.env` and
  `docker compose up -d`. Give a rebuilt machine a new host name, and so a
  new key.
* **Back up** `./secrets/master.key` (owned by the container's prosody user,
  so read it with sudo) and the `data` volume (accounts, archives, rooms).
  Losing the master key invalidates every host key. The `letsencrypt` volume
  can be recreated.
* **Logs**: `docker compose logs`. Refused agent logins are logged with the
  reason.
* **Who can send what**: only users of this server — people with accounts,
  and agents on hosts holding a key. The push services in
  `S2S_ALLOWED_DOMAINS` can connect, but only to receive notifications. An
  agent JID can receive mail only once it has logged in. See `../README.md`.

## Files

| File | |
|---|---|
| `Dockerfile` | the official `prosodyim/prosody:13.0` image + certbot + the module |
| `docker-compose.yaml` | ports, volumes, settings from `.env` |
| `.env.example` | the settings, with placeholder names |
| `prosody.cfg.lua` | Prosody's configuration, driven by the environment |
| `xmpp-entrypoint` | first start: master key, certificates, renewal loop |
| `xmpp-certs` | `obtain` / `renew` / `deploy` (certbot + `prosodyctl cert import`) |
| `xmpp-mcp-host-key` | derive a host's key (same result as `xmpp-mcp-keys host-key`) |
| `../mod_agent_roster.lua` | agents in people's contact lists and vice versa (the build copies it from `XMPP_MCP_MODULE_DIR`, with the auth module) |
| `mod_s2s_whitelist.lua` | limits server-to-server to `S2S_ALLOWED_DOMAINS` (vendored from prosody-modules) |
| `apache/jabber-acme.conf`, `caddy/jabber-acme.Caddyfile` | forward the ACME challenge from port 80 |
