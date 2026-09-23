# Prosody 13 for people and xmpp-mcp agents, with its own Let's Encrypt
# certificates. Built from contrib/prosody/ (see docker-compose.yaml):
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

COPY mod_auth_xmpp_mcp.lua /etc/prosody/modules/
COPY deploy/prosody.cfg.lua /etc/prosody/prosody.cfg.lua
COPY deploy/xmpp-entrypoint deploy/xmpp-certs deploy/xmpp-mcp-host-key /usr/local/bin/
RUN chmod 0755 /usr/local/bin/xmpp-entrypoint /usr/local/bin/xmpp-certs \
               /usr/local/bin/xmpp-mcp-host-key

# Port 80 is only for certbot's HTTP-01 answers (published on localhost and
# reached through the host's Apache); 5222 is XMPP.
EXPOSE 80 5222

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/xmpp-entrypoint"]
CMD ["prosody", "-F"]
