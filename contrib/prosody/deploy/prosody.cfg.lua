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
-- xmpp-certs (one per client domain). Server-to-server is off, except to the
-- domains in S2S_ALLOWED_DOMAINS (phone push services): only users of this
-- server can send anything to anyone on it.

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

-- Server-to-server, only to S2S_ALLOWED_DOMAINS. Phones need it for push
-- (XEP-0357): the server wakes the app through its vendor's push service
-- (Monal: eu.prod.push.monal-im.org), which it reaches over s2s. Every other
-- domain is refused in both directions by mod_s2s_whitelist; with the list
-- empty, s2s is not loaded at all.
local s2s_allowed = list(ENV_S2S_ALLOWED_DOMAINS)
if #s2s_allowed > 0 then
	modules_enabled:append({ "s2s_whitelist"; "dialback"; "s2s_bidi" })
	s2s_whitelist = s2s_allowed
	s2s_require_encryption = true
else
	modules_disabled = { "s2s" }
end

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

-- People and the agents in the Everyone room in each other's contact lists,
-- named by the agents' current names and kept up to date as they rename
-- (mod_agent_roster, loaded on both hosts below). EVERYONE_ROOM=off turns it off;
-- unset or empty (as Compose passes an unset one) means everyone@MUC_DOMAIN.
local everyone_room = (ENV_EVERYONE_ROOM and ENV_EVERYONE_ROOM ~= "") and ENV_EVERYONE_ROOM
	or ("everyone@" .. muc_domain)
local agent_roster = everyone_room ~= "off" and "agent_roster" or nil
agent_roster_room = everyone_room
agent_roster_people_host = jabber_domain
agent_roster_agents_host = agent_domain

-- Certificates are imported into /etc/prosody/certs (<domain>.crt / .key) by
-- xmpp-certs, where Prosody finds them by name.
certificates = "certs"

VirtualHost (jabber_domain)
	-- Push notifications for people's phones (XEP-0357). A push carries no
	-- message text and no sender: it only wakes the app, which then fetches
	-- the message over its own connection.
	modules_enabled = { "cloud_notify"; agent_roster }

VirtualHost (agent_domain)
	authentication = "xmpp_mcp"
	modules_enabled = { agent_roster }
	xmpp_mcp_master_key_file = "/etc/prosody/secrets/master.key"
	-- Hosts whose key must stop working (a leaked key, a retired machine).
	xmpp_mcp_revoked_hosts = list(ENV_XMPP_MCP_REVOKED_HOSTS)
	-- The room service hangs off the people's domain, so agents can't find it
	-- by walking up from theirs; list it in this host's disco#items instead.
	disco_items = {
		{ muc_domain, "Chat rooms" };
	}

Component (muc_domain) "muc"
	modules_enabled = { "muc_mam"; #s2s_allowed > 0 and "s2s_whitelist" or nil }
	-- PROSODY_ADMINS administer every room: owners, whoever created it.
	-- (Prosody 13 does not make server admins room owners without this.)
	component_admins_as_room_owners = true
	-- Anyone with an account (people and agents) may create a room; agents
	-- create them simply by joining, so rooms are usable at once.
	restrict_room_creation = false
	muc_room_locking = false
	muc_room_default_public = true
	muc_room_default_persistent = true
	-- Occupants see each other's real JIDs: xmpp-mcp's sender gate and
	-- list_agents rely on it.
	muc_room_default_public_jids = true
