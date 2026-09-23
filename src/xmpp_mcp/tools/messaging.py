"""Direct (1:1) messaging tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..security_labels import SecurityLabelError, build_label
from ..xmpp_client import XMPPError
from . import get_xmpp


def register(mcp: FastMCP) -> None:
    """Register direct-messaging tools on the FastMCP app."""

    @mcp.tool
    async def send_message(
        ctx: Context,
        to: Annotated[
            str,
            Field(
                description=(
                    "Recipient: a JID (alice@example.com) or the friendly name / "
                    "agent ID of a peer shown by list_agents"
                )
            ),
        ],
        body: Annotated[str, Field(description="Message text to send")],
        security_label: Annotated[
            str | None,
            Field(
                description=(
                    "Optional XEP-0258 security label selector (Isode M-Link). Must be "
                    "a selector previously returned by list_security_labels for this "
                    "recipient."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Send a 1:1 chat message to an XMPP user or agent.

        `to` may be a name rather than a JID; it must identify exactly one
        peer. For M-Link servers that enforce security labels, pass
        `security_label` with a selector obtained from `list_security_labels`.

        Async so it runs on the event loop: resolving a name reads presence
        state that the XML stream updates on that loop.
        """
        xmpp = get_xmpp(ctx)
        try:
            to = xmpp.resolve_address(to)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        label = None
        if security_label:
            try:
                label = build_label(to, security_label)
            except SecurityLabelError as exc:
                raise ToolError(str(exc)) from exc
        try:
            xmpp.send_chat(to, body, label=label)
        except XMPPError as exc:
            raise ToolError(str(exc)) from exc
        return {"sent": True, "to": to, "security_label": security_label}

    @mcp.tool
    def search_messages(
        ctx: Context,
        query: Annotated[
            str | None,
            Field(description="Case-insensitive substring to look for in the message body"),
        ] = None,
        room: Annotated[
            str | None,
            Field(description="Filter to messages from one MUC room (bare JID)"),
        ] = None,
        participant: Annotated[
            str | None,
            Field(
                description=(
                    "Filter to messages from one sender. Pass a bare JID "
                    "(e.g. `alice@xmpp.test`) for 1:1 chats or a MUC nickname "
                    "(e.g. `alice`) for group chats."
                )
            ),
        ] = None,
        since: Annotated[
            str | None,
            Field(
                description="ISO 8601 timestamp; only return messages received at or after this time"
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum number of results", ge=1, le=500)
        ] = 50,
    ) -> dict[str, Any]:
        """Search the bot's inbound message buffer **without draining it**.

        Combine `query`, `room`, and `participant` to answer questions like
        "who is talking about X in #engineering?" or "what did Alice say
        recently?" Repeat calls see the same messages until they age out of
        the buffer (default size 500, oldest dropped first).
        """
        xmpp = get_xmpp(ctx)
        matches = xmpp.search_inbox(
            query=query, room=room, participant=participant, since=since, limit=limit
        )
        return {"count": len(matches), "messages": matches}

    @mcp.tool
    def get_recent_messages(
        ctx: Context,
        from_jid: Annotated[
            str | None,
            Field(description="If set, only return messages from this bare JID"),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum number of messages to return", ge=1, le=200)
        ] = 20,
    ) -> dict[str, Any]:
        """Retrieve buffered inbound messages received since the last call.

        Messages are removed from the buffer once returned. Includes the XEP-0258
        display marking under `security_label` when the sender applied one.
        """
        xmpp = get_xmpp(ctx)
        messages = xmpp.drain_inbox(from_jid=from_jid, limit=limit)
        return {"count": len(messages), "messages": messages}
