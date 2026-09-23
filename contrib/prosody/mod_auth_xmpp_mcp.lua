-- mod_auth_xmpp_mcp: host-scoped derived credentials for xmpp-mcp agents.
--
-- The server half of xmpp_mcp/credentials.py; see README.md alongside. Agents
-- on a virtual host that uses this module log in as <session>.<host>@<vhost>
-- with a password they
-- derive from their *host's* key; the server holds only a master key and
-- re-derives:
--
--   host_key = HMAC-SHA256(master,   "xmpp-mcp host v1|"  .. host)
--   mac      = HMAC-SHA256(host_key, "xmpp-mcp agent v1|" .. bare_jid .. "|" .. expiry)
--   password = "xmc1." .. expiry .. "." .. base64url(mac)
--
-- `host` is taken from the JID being authenticated (after the first dot), so a
-- host's key only ever works for that host's JIDs. There are no accounts to
-- create or change. An agent JID "exists" — can receive messages, and have
-- them kept while it is offline — once it has logged in at least once. So
-- nobody can have mail stored for a session ID they merely made up: only a
-- holder of that host's key could bring such a JID into being, by logging in
-- as it.
--
-- Only PLAIN is offered — the password has to reach us to be checked — so the
-- host must require TLS (c2s_require_encryption, which is Prosody's default).
--
-- Options:
--   xmpp_mcp_master_key_file  path to the master key (64 hex characters)
--   xmpp_mcp_max_ttl          longest credential lifetime accepted (seconds, 7 days)
--   xmpp_mcp_clock_skew       tolerance for a just-expired credential (seconds, 300)
--   xmpp_mcp_revoked_hosts    hosts whose key must no longer work, e.g. { "host1" }
--
-- Revocation: host keys are derived from the master, so without this list the
-- only way to cut off one compromised host would be a new master key — which
-- re-keys every host. Revoke the host here instead, and give the machine a new
-- host name (and so a new key) when it is rebuilt.

local new_sasl = require "prosody.util.sasl".new;
local hashes = require "prosody.util.hashes";
local hex = require "prosody.util.hex";
local base64 = require "prosody.util.encodings".base64;

local host = module.host;
local max_ttl = module:get_option_number("xmpp_mcp_max_ttl", 7 * 24 * 3600);
local skew = module:get_option_number("xmpp_mcp_clock_skew", 300);
local master_file = module:get_option_string("xmpp_mcp_master_key_file");
local revoked = module:get_option_set("xmpp_mcp_revoked_hosts", {});
-- Agent JIDs that have logged in: the only ones that "exist" (see above).
local seen = module:open_store("xmpp_mcp_seen");

local function load_master()
	assert(master_file, "mod_auth_xmpp_mcp: set xmpp_mcp_master_key_file");
	local f = assert(io.open(master_file, "r"));
	local text = f:read("*a"):gsub("%s", "");
	f:close();
	local key = hex.decode(text);
	assert(key and #key == 32, "mod_auth_xmpp_mcp: master key must be 64 hex characters");
	return key;
end
local master = load_master();

local function base64url(data)
	return (base64.encode(data):gsub("%+", "-"):gsub("/", "_"):gsub("=", ""));
end

-- "<session>.<host>" -> session, host   (nil for anything else)
local function split(username)
	if type(username) ~= "string" then return nil; end
	return username:match("^([^.]+)%.(.+)$");
end

local provider = { name = "xmpp_mcp" };

-- Every refusal is logged with its reason: "expired" usually means a clock
-- out of step with the server, "bad credential" a wrong or foreign host key.
local function refuse(username, reason)
	module:log("info", "Rejected credential for %s@%s: %s", tostring(username), host, reason);
	return nil, reason;
end

function provider.test_password(username, password)
	local _, agent_host = split(username);
	if not agent_host or type(password) ~= "string" then
		return refuse(username, "not an agent JID");
	end
	if revoked:contains(agent_host) then
		return refuse(username, "host revoked");
	end
	local expiry, token = password:match("^xmc1%.(%d+)%.([%w%-_]+)$");
	if not expiry then
		return refuse(username, "not a derived credential");
	end
	local now = os.time();
	expiry = tonumber(expiry);
	if expiry < now - skew then
		return refuse(username, "credential expired");
	end
	if expiry > now + max_ttl then
		return refuse(username, "credential lifetime too long");
	end
	local host_key = hashes.hmac_sha256(master, "xmpp-mcp host v1|" .. agent_host);
	local jid = username .. "@" .. host;
	local mac = hashes.hmac_sha256(host_key, "xmpp-mcp agent v1|" .. jid .. "|" .. expiry);
	if hashes.equals(base64url(mac), token) then
		if not seen:get(username) then
			seen:set(username, { first_login = now });
		end
		return true;
	end
	return refuse(username, "bad credential");
end

function provider.user_exists(username)
	return split(username) ~= nil and seen:get(username) ~= nil;
end

function provider.get_password()
	return nil, "passwords are derived, not stored";
end

function provider.set_password()
	return nil, "passwords are derived from the host key";
end

function provider.create_user()
	return nil, "agent accounts are implicit";
end

function provider.delete_user()
	return nil, "agent accounts are implicit";
end

function provider.users()
	return function() return nil; end;
end

function provider.get_sasl_handler()
	return new_sasl(host, {
		plain_test = function(_, username, password)
			return provider.test_password(username, password), true;
		end;
	});
end

module:add_item("auth-provider", provider);
