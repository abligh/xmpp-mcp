-- Prosody for people and xmpp-mcp agents. Every site-specific value comes
-- from the environment (docker-compose.yaml / .env); Prosody 13 exposes each
-- environment variable FOO to this file as ENV_FOO.
--
--   JABBER_DOMAIN   people: ordinary accounts, SCRAM-hashed passwords
--   AGENT_DOMAIN    agents: <session>.<host>@AGENT_DOMAIN, no accounts,
--                   host-scoped derived credentials (mod_auth_xmpp_mcp)
--   MUC_DOMAIN      rooms, shared by both
--
-- TLS is required for every client; certificates come from Let's Encrypt via
-- xmpp-certs (one per client domain). Server-to-server is off: only users of
-- this server can send anything to anyone on it.

local function list(s)
	local parts = {};
	for part in (s or ""):gmatch("[^,%s]+") do
		parts[#parts + 1] = part;
	end
	return parts;
end

local jabber_domain = Lua.assert(ENV_JABBER_DOMAIN, "JABBER_DOMAIN is not set");
local agent_domain = Lua.assert(ENV_AGENT_DOMAIN, "AGENT_DOMAIN is not set");
local muc_domain = Lua.assert(ENV_MUC_DOMAIN, "MUC_DOMAIN is not set");

admins = list(ENV_PROSODY_ADMINS)

plugin_paths = { "/etc/prosody/modules" }

modules_enabled = {
	"roster"; "saslauth"; "tls"; "disco"; "ping"; "time"; "uptime"; "version";
	"carbons"; "smacks"; "csi_simple"; "blocklist"; "bookmarks";
	"pep"; "private"; "vcard4"; "vcard_legacy";
	"offline"; "mam"; "limits"; "admin_shell";
}
modules_disabled = { "s2s" }

allow_registration = false
c2s_require_encryption = true
authentication = "internal_hashed"
storage = "internal"

-- Message archive (XEP-0313), kept for this long.
archive_expires_after = (ENV_ARCHIVE_EXPIRY or "4w")

limits = {
	c2s = { rate = "50kb/s"; burst = "2s" };
}

log = {
	{ levels = { min = ENV_LOG_LEVEL or "info" }, to = "console" };
}

-- Certificates are imported into /etc/prosody/certs (<domain>.crt / .key) by
-- xmpp-certs, where Prosody finds them by name.
certificates = "certs"

VirtualHost (jabber_domain)

VirtualHost (agent_domain)
	authentication = "xmpp_mcp"
	xmpp_mcp_master_key_file = "/etc/prosody/secrets/master.key"
	-- Hosts whose key must stop working (a leaked key, a retired machine).
	xmpp_mcp_revoked_hosts = list(ENV_XMPP_MCP_REVOKED_HOSTS)
	-- The room service hangs off the people's domain, so agents can't find it
	-- by walking up from theirs; list it in this host's disco#items instead.
	disco_items = {
		{ muc_domain, "Chat rooms" };
	}

Component (muc_domain) "muc"
	modules_enabled = { "muc_mam" }
	-- Anyone with an account (people and agents) may create a room; agents
	-- create them simply by joining, so rooms are usable at once.
	restrict_room_creation = false
	muc_room_locking = false
	muc_room_default_public = true
	muc_room_default_persistent = true
	-- Occupants see each other's real JIDs: xmpp-mcp's sender gate and
	-- list_agents rely on it.
	muc_room_default_public_jids = true
