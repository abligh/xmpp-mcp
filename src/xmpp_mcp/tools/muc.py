"""Multi-User Chat (MUC / group chat) tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..security_labels import SecurityLabelError, build_label
from ..xmpp_client import XMPPError
from . import get_xmpp


def register(mcp: FastMCP) -> None:
    """Register MUC tools on the FastMCP app."""

    @mcp.tool
    async def join_room(
        ctx: Context,
        room_jid: Annotated[
            str, Field(description="Room JID, e.g. project@conference.example.com")
        ],
        nick: Annotated[
            str | None,
            Field(description="Nickname to use in the room. Defaults to XMPP_NICK."),
        ] = None,
    ) -> dict[str, Any]:
        """Join a Multi-User Chat room. Required before sending to or reading it."""
        xmpp = get_xmpp(ctx)
        try:
            return await xmpp.join_room(room_jid, nick=nick)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    def leave_room(
        ctx: Context,
        room_jid: Annotated[str, Field(description="Room JID to leave")],
    ) -> dict[str, Any]:
        """Leave a previously joined Multi-User Chat room."""
        xmpp = get_xmpp(ctx)
        try:
            xmpp.leave_room(room_jid)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        return {"left": True, "room": room_jid}

    @mcp.tool
    def send_room_message(
        ctx: Context,
        room_jid: Annotated[str, Field(description="Room JID to send to (must be joined)")],
        body: Annotated[str, Field(description="Message text to send to the room")],
        security_label: Annotated[
            str | None,
            Field(
                description=(
                    "Optional XEP-0258 security label selector (Isode M-Link). Must be "
                    "a selector previously returned by list_security_labels for this room."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Send a message to a joined MUC room, optionally with an M-Link security label."""
        xmpp = get_xmpp(ctx)
        label = None
        if security_label:
            try:
                label = build_label(room_jid, security_label)
            except SecurityLabelError as exc:
                raise ToolError(str(exc)) from exc
        try:
            xmpp.send_groupchat(room_jid, body, label=label)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        return {"sent": True, "room": room_jid, "security_label": security_label}

    @mcp.tool
    async def list_rooms(
        ctx: Context,
        service: Annotated[
            str | None,
            Field(
                description=(
                    "MUC service to list, e.g. conference.example.com. Omit to "
                    "find every room service on the server."
                )
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rooms to describe", ge=1, le=500)
        ] = 50,
    ) -> dict[str, Any]:
        """List chat rooms you could join, and the ones you are in.

        Per room: `room` (JID to pass to join_room), `name`, `description`,
        `occupants` (count), `joined` / `nick`, and flags (`public`,
        `members_only`, `password_protected`, `anonymous`, `persistent`).
        Servers list only public rooms; rooms you have joined always appear.
        """
        xmpp = get_xmpp(ctx)
        try:
            return await xmpp.list_rooms(service, limit=limit)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def list_room_occupants(
        ctx: Context,
        room_jid: Annotated[str, Field(description="Room JID to inspect")],
    ) -> dict[str, Any]:
        """List who is in a room.

        In a room you have joined: every occupant's `nick`, `role`,
        `affiliation`, real `jid` (where the room discloses it), and for agents
        their friendly `name`, `agent_id` and `presence` / `status`; `me` marks
        you. In a room you have not joined: just the nicks, if the room is
        willing to share them — `hidden: true` (with `occupant_count`) means
        it isn't, so join to see who is there.
        """
        xmpp = get_xmpp(ctx)
        try:
            members = await xmpp.room_members(room_jid)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        return {**members, "count": len(members["occupants"])}
