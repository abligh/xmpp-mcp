# Prosody 13 for people and xmpp-mcp agents, with its own Let's Encrypt
# certificates. Built from this directory, plus the directory holding
# mod_auth_xmpp_mcp.lua as the build context "module" (XMPP_MCP_MODULE_DIR in
# .env; by default the parent, contrib/prosody/):
#
#   docker compose build
#
# Adds to the official image: certbot, the agent auth module, a config driven
# by environment variables, and a start-up script that sets up the master key
# and certificates before Prosody starts. See README.md.

FROM prosodyim/prosody:13.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends certbot \
 && rm -rf /var/lib/apt/lists/*

COPY --from=module mod_auth_xmpp_mcp.lua mod_agent_roster.lua /etc/prosody/modules/
COPY prosody.cfg.lua /etc/prosody/prosody.cfg.lua
COPY mod_s2s_whitelist.lua /etc/prosody/modules/
COPY xmpp-entrypoint xmpp-certs xmpp-mcp-host-key /usr/local/bin/
RUN chmod 0755 /usr/local/bin/xmpp-entrypoint /usr/local/bin/xmpp-certs \
               /usr/local/bin/xmpp-mcp-host-key

# Port 80 is only for certbot's HTTP-01 answers (published on localhost and
# reached through the host's web server); 5222 is XMPP clients, 5269 other
# servers (only those in S2S_ALLOWED_DOMAINS).
EXPOSE 80 5222 5269

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/xmpp-entrypoint"]
CMD ["prosody", "-F"]
