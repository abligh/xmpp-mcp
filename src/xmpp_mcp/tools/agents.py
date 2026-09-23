"""Agent-to-agent tools: ``reply``, ``list_agents`` and ``get_identity``.

These are the XMPP counterparts of Claude Code's local ``SendMessage`` /
``ListAgents``: they work across containers, hosts, model providers and
billing accounts because the only thing peers share is an XMPP server.

The tools are ``async`` so FastMCP runs them on the event loop rather than in
a worker thread: they touch slixmpp state (the send queue, the room roster,
the presence cache) that the XML stream mutates on that loop.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..xmpp_client import XMPPError, parse_jid
from . import get_settings, get_xmpp


def register(mcp: FastMCP) -> None:
    """Register agent-to-agent tools on the FastMCP app."""

    @mcp.tool
    async def reply(
        ctx: Context,
        to: Annotated[
            str,
            Field(
                description=(
                    "Where to send the reply: normally the `reply_to` attribute "
                    "of the <channel> tag you are answering. A joined room's JID "
                    "posts to the room; any other JID (user@domain, "
                    "user@domain/resource, or room@service/nick for a private "
                    "message to a room occupant) sends a one-to-one message. A "
                    "peer's friendly name or agent ID (see list_agents) also works."
                )
            ),
        ],
        message: Annotated[str, Field(description="The text to send")],
        thread: Annotated[
            str | None,
            Field(description="The `thread` attribute of the message being answered, if any"),
        ] = None,
    ) -> dict[str, Any]:
        """Reply to a message that arrived over the XMPP channel.

        Routes automatically: to a room you have joined it sends a groupchat
        message (seen by every occupant); to anything else it sends a
        one-to-one chat message.
        """
        xmpp = get_xmpp(ctx)
        if not message:
            raise ToolError("message must not be empty")
        try:
            target = parse_jid(xmpp.resolve_address(to), "reply address")
            if not target.resource and xmpp.is_joined(target.bare):
                xmpp.send_groupchat(target.bare, message, thread=thread)
                kind = "groupchat"
            elif not target.resource and xmpp.is_known_room(target.bare):
                # A room we are not in right now (left, kicked, or mid
                # reconnect). Sending a 1:1 chat to a bare room JID is silently
                # dropped by the service (XEP-0045 §7.9), so say so instead of
                # reporting a delivery that never happens.
                raise ToolError(
                    f"Not joined to room {target.bare} — call join_room first"
                )
            else:
                xmpp.send_chat(target.full, message, thread=thread)
                kind = "chat"
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        return {"sent": True, "to": target.full, "type": kind}

    @mcp.tool
    async def list_agents(
        ctx: Context,
        include_offline: Annotated[
            bool,
            Field(description="Include roster contacts who are currently offline"),
        ] = True,
        agents_only: Annotated[
            bool,
            Field(
                description=(
                    "Only list peers that advertise themselves as agents "
                    "(xmpp-mcp presence metadata); hides plain human clients"
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """List the agents and people reachable over XMPP, with presence.

        Covers roster contacts and occupants of every joined room (join a
        shared directory room — see XMPP_AUTO_JOIN — to see every agent on
        the network). Per peer: `jid` (canonical XMPP address), `agent_id`
        (the peer's internal Claude Code session ID), `name` (friendly,
        human-facing name — it can change, and need not be unique;
        `name_source` says how it came about), `presence` / `status`, and
        `address` — what to pass to `reply` or `send_message` to reach it.
        Those tools also accept a `name` directly when it is unambiguous.
        """
        xmpp = get_xmpp(ctx)
        agents = xmpp.list_agents(include_offline=include_offline, agents_only=agents_only)
        return {"count": len(agents), "agents": agents}

    @mcp.tool
    async def get_identity(ctx: Context) -> dict[str, Any]:
        """Report this agent's own XMPP identity — the address peers use to reach it."""
        xmpp = get_xmpp(ctx)
        s = get_settings(ctx)
        bound = xmpp.xmpp.boundjid
        session = s.claude_session
        return {
            "jid": bound.bare,
            "full_jid": bound.full,
            "agent_name": s.xmpp_agent_name,
            "agent_id": s.xmpp_agent_id,
            # The friendly name peers see; follows the Claude Code session's
            # name unless XMPP_DISPLAY_NAME pins it.
            "name": xmpp.friendly_name,
            "name_source": xmpp.name_source,
            "claude_session": session.session_id if session else None,
            # How the session file was found: CLAUDE_PID, parent process,
            # session id, or XMPP_CLAUDE_SESSION — useful when it wasn't.
            "claude_session_found_via": session.how if session else None,
            "host": s.agent_host,
            "nick": s.xmpp_nick,
            "rooms": [{"room": r, "nick": xmpp.nick_in(r)} for r in xmpp.joined_rooms],
        }
