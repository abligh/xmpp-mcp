-- mod_agent_roster: people and agents in each other's contact lists, by name.
--
-- Load it on both VirtualHosts, the people's and the agents'. It puts
--   * every agent seen in the Everyone room into every person's roster, in
--     the group "Agents", named by the agent's nick in that room (which
--     xmpp-mcp keeps equal to the agent's friendly name), and
--   * every person (every account on this host) into every agent's roster,
--     in the group "People",
-- both with subscription "both", so presence flows each way. Joins, leaves
-- and renames are pushed to connected clients as they happen: renaming an
-- agent renames it in everyone's contact list.
--
-- Nothing is stored in anyone's roster: the entries are added as a roster
-- loads, marked persist=false, and kept out of what is saved (as mod_groups
-- does). An agent that leaves the room stays listed, shown offline, for
-- agent_roster_keep seconds, so a restart does not make it flicker away.
--
-- Options (global, so both hosts see them):
--   agent_roster_room          the Everyone room, e.g. "everyone@conference.example.com"
--   agent_roster_people_host   the people's VirtualHost, e.g. "example.com"
--   agent_roster_agents_host   the agents' VirtualHost, e.g. "agents.example.com"
--   agent_roster_keep          seconds an absent agent stays listed (7 days)
--   agent_roster_agents_group  group name for agents  ("Agents")
--   agent_roster_people_group  group name for people  ("People")

local datamanager = require "prosody.util.datamanager";
local jid_split = require "prosody.util.jid".split;
local jid_bare = require "prosody.util.jid".bare;
local rostermanager = require "prosody.core.rostermanager";
local usermanager = require "prosody.core.usermanager";

local room_jid = module:get_option_string("agent_roster_room");
local people_host = module:get_option_string("agent_roster_people_host");
local agents_host = module:get_option_string("agent_roster_agents_host");
local keep = module:get_option_number("agent_roster_keep", 7 * 24 * 3600);
local agents_group = module:get_option_string("agent_roster_agents_group", "Agents");
local people_group = module:get_option_string("agent_roster_people_group", "People");
assert(room_jid, "mod_agent_roster: set agent_roster_room");
assert(agents_host, "mod_agent_roster: set agent_roster_agents_host");
assert(people_host, "mod_agent_roster: set agent_roster_people_host");
local on_people_host = module.host == people_host;
if not on_people_host and module.host ~= agents_host then
	module:log("warn", "loaded on %s, which is neither %s nor %s: doing nothing",
		module.host, people_host, agents_host);
	return;
end
local _, muc_host = jid_split(room_jid);

-- The agents we list: bare JID -> { name = nick, seen = time, present = bool }.
local store = module:open_store("agent_roster");
local agents = store:get("agents") or {};

local function item(name, group)
	return { subscription = "both", name = name, groups = { [group] = true }, persist = false };
end

-- --- injecting, as rosters load -------------------------------------------------

local function inject_agents(roster)
	for jid, a in pairs(agents) do
		roster[jid] = item(a.name, agents_group);
	end
	roster[false] = roster[false] or {};
	roster[false].version = true; -- not versionable: its contents change underneath
end

local function inject_people(roster)
	for username in usermanager.users(people_host) do
		local jid = username .. "@" .. people_host;
		roster[jid] = item(username, people_group);
	end
	roster[false] = roster[false] or {};
	roster[false].version = true;
end

-- Each host's own instance fills its own users' rosters as they load.
module:hook("roster-load", function(event)
	if on_people_host then inject_agents(event.roster); else inject_people(event.roster); end
end);

-- Never save the injected entries.
local function strip(username, host, datastore, data)
	if datastore == "roster" and host == module.host and type(data) == "table" then
		local kept = {};
		for jid, contact in pairs(data) do
			if jid == false or contact.persist ~= false then
				kept[jid] = contact;
			end
		end
		if kept[false] then kept[false].version = nil; end
		return username, host, datastore, kept;
	end
	return username, host, datastore, data;
end
datamanager.add_callback(strip);
function module.unload()
	datamanager.remove_callback(strip);
end

-- --- keeping connected clients up to date ------------------------------------------

local function push(host, jid, make)
	-- make(username) -> the item for jid in that user's roster, or nil to remove it.
	local host_session = prosody.hosts[host];
	if not host_session then return; end
	for username, user in pairs(host_session.sessions) do
		if user.roster then
			user.roster[jid] = make(username);
			rostermanager.roster_push(username, host, jid);
		end
	end
end

local function refresh()
	local mod_muc = prosody.hosts[muc_host] and prosody.hosts[muc_host].modules.muc;
	local room = mod_muc and mod_muc.get_room_from_jid(room_jid);
	local now = os.time();
	local present = {};
	if room then
		for _, occupant in room:each_occupant() do
			local bare = jid_bare(occupant.bare_jid);
			local _, host = jid_split(bare);
			if host == agents_host then
				local _, _, nick = jid_split(occupant.nick);
				present[bare] = nick;
			end
		end
	end
	local changed = {};
	for bare, nick in pairs(present) do
		local a = agents[bare];
		if not a or a.name ~= nick or not a.present then
			changed[bare] = not a or a.name ~= nick;
		end
		agents[bare] = { name = nick, seen = now, present = true };
	end
	for bare, a in pairs(agents) do
		if not present[bare] then
			if a.present then
				a.present = false;
				a.seen = now;
			elseif now - (a.seen or 0) > keep then
				agents[bare] = nil;
				changed[bare] = true;
			end
		end
	end
	local any = false;
	for bare, roster_changed in pairs(changed) do
		any = true;
		if roster_changed then
			local a = agents[bare];
			module:log("info", "%s %s", bare, a and ("is now listed as " .. a.name) or "removed from rosters");
			push(people_host, bare, function() return a and item(a.name, agents_group) or nil; end);
		end
	end
	if any then store:set("agents", agents); end
end

if not on_people_host then return; end  -- the rest watches the room, for people

module:context(muc_host):hook("muc-occupant-joined", function() module:add_timer(0, refresh); end);
module:context(muc_host):hook("muc-occupant-left", function() module:add_timer(0, refresh); end);
-- A nick change fires neither: look again every few seconds.
module:add_timer(5, function() refresh(); return 5; end);

-- People come and go rarely; tell connected agents when they do.
module:hook("user-registered", function(event)
	local jid = event.username .. "@" .. people_host;
	push(agents_host, jid, function() return item(event.username, people_group); end);
end);
module:hook("user-deleted", function(event)
	push(agents_host, event.username .. "@" .. people_host, function() return nil; end);
end);
