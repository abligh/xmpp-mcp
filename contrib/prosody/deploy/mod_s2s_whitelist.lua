-- mod_s2s_whitelist, from prosody-modules (MIT licence, Copyright (c) Kim
-- Alvefur, Menel and contributors), vendored unchanged apart from this header:
--   https://hg.prosody.im/prosody-modules/file/tip/mod_s2s_whitelist
-- Fetched 2026-09-24. Pinned here rather than installed at build time: it is
-- the boundary that keeps every server but those listed in s2s_whitelist
-- (S2S_ALLOWED_DOMAINS) away from this one, in both directions.
--
-- Outgoing: stanzas for any other domain are bounced with not-allowed.
-- Incoming: a stream from any other domain (or naming none) is closed with
-- policy-violation before authentication.

local st = require "util.stanza";

local whitelist = module:get_option_inherited_set("s2s_whitelist", {});

module:hook("route/remote", function (event)
	if not whitelist:contains(event.to_host) then
		module:send(st.error_reply(event.stanza, "cancel", "not-allowed", "Communication with this domain is restricted"));
		return true;
	end
end, 100);

module:hook("s2s-stream-features", function (event)
	if not whitelist:contains(event.origin.from_host) then
		event.origin:close({
			condition = "policy-violation";
			text = "Communication with this domain is restricted";
		});
	end
end, 1000);
