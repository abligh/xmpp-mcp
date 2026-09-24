-- Prosody lab for xmpp-mcp: humans and agents on one server, authenticated
-- differently.
--
--   xmpp.test          humans — ordinary accounts, SCRAM-hashed passwords
--   agents.xmpp.test   agents — <session>.<host>@agents.xmpp.test, no stored
--                      accounts: host-scoped derived credentials checked by
--                      mod_auth_xmpp_mcp (contrib/prosody/)
--   conference.xmpp.test  rooms shared by both
--
-- TLS is mandatory for every client (the derived credential travels as SASL
-- PLAIN). The lab's certificate comes from a throwaway CA created by
-- start-lab-prosody.py; clients verify it with XMPP_CA_FILE.
--
-- In production: real certificates, the master key owned by the prosody user
-- with mode 0600, and the agents host on its own DNS name.

admins = { }

plugin_paths = { "/etc/prosody/modules" }

modules_enabled = {
	"roster"; "saslauth"; "tls"; "disco"; "carbons"; "pep"; "private";
	"vcard4"; "vcard_legacy"; "version"; "uptime"; "time"; "ping";
	"offline"; "mam"; "admin_shell";
}
modules_disabled = { "s2s" }

allow_registration = false
c2s_require_encryption = true
authentication = "internal_hashed"
storage = "internal"

ssl = {
	certificate = "/etc/prosody/certs/lab.crt";
	key = "/etc/prosody/certs/lab.key";
}

-- Keep every message in the archive by default (lab: lets MAM be inspected).
default_archive_policy = true

log = {
	{ levels = { min = "info" }, to = "console" };
}

-- People and the agents in the Everyone room in each other's contact lists,
-- named and kept current (contrib/prosody/mod_agent_roster.lua, loaded on both
-- hosts below).
agent_roster_room = "everyone@conference.xmpp.test"
agent_roster_people_host = "xmpp.test"
agent_roster_agents_host = "agents.xmpp.test"

VirtualHost "xmpp.test"
	modules_enabled = { "agent_roster" }

VirtualHost "agents.xmpp.test"
	modules_enabled = { "agent_roster" }
	authentication = "xmpp_mcp"
	xmpp_mcp_master_key_file = "/etc/prosody/secrets/master.key"
	-- A host whose key has leaked: its credentials are refused even though
	-- they are correctly derived. (The lab keeps one, to prove it.)
	xmpp_mcp_revoked_hosts = { "revokedhost" }

Component "conference.xmpp.test" "muc"
	modules_enabled = { "muc_mam" }
	restrict_room_creation = false
	-- New rooms are usable at once (XEP-0045 instant rooms); agents create
	-- directory rooms simply by joining them.
	muc_room_locking = false
	muc_room_default_public = true
	muc_room_default_persistent = true
	-- Non-anonymous: occupants see each other's real JIDs, which the
	-- channel's sender gate and list_agents rely on.
	muc_room_default_public_jids = true
