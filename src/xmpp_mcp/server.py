"""FastMCP server wiring for xmpp-mcp.

Builds the FastMCP app, manages the XMPP connection lifecycle in the server
lifespan, and registers every tool/resource module.

With channel mode on (``--channel`` / ``XMPP_CHANNEL=true``) the app also
declares the ``claude/channel`` capability and pushes inbound messages to
Claude Code — see :mod:`xmpp_mcp.channel`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .channel import (
    CHANNEL_CAPABILITY, ChannelBridge, ChannelSessionMiddleware, SenderGate,
    channel_instructions,
)
from .config import Settings, load_settings
from .openfire_admin import OpenfireAdmin
from .tools import (
    CTX_CHANNEL, CTX_OPENFIRE, CTX_SETTINGS, CTX_XMPP,
    admin, agents, disco, mam, messaging, muc, presence, pubsub,
)
from .xmpp_client import XMPPClient

logger = logging.getLogger("xmpp_mcp")

_BASE_INSTRUCTIONS = (
    "Operate over XMPP: send/receive direct and MUC messages, manage "
    "presence and rosters, discover server features, and apply XEP-0258 "
    "security labels (Isode M-Link). When OPENFIRE_* is configured, the "
    "of_* tools administer an Openfire server over its REST API."
)


def _make_lifespan(
    settings: Settings,
) -> Callable[[FastMCP], AbstractAsyncContextManager[dict[str, Any]]]:
    @asynccontextmanager
    async def _lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Open the XMPP connection (and optional Openfire client) for the server's life."""
        xmpp = XMPPClient(settings)
        bridge: ChannelBridge | None = None
        if settings.xmpp_channel:
            # Register the listener before connecting so nothing that arrives
            # during login (e.g. offline messages) is missed; it waits in the
            # bridge queue until the MCP client has initialised.
            bridge = ChannelBridge(
                SenderGate(settings.channel_allow_patterns),
                max_pending=settings.xmpp_inbox_size,
            )
            xmpp.add_message_listener(bridge.submit)
            logger.info(
                "Channel mode on — pushing messages from %s",
                ", ".join(bridge.gate.patterns),
            )
        await xmpp.start()

        openfire = OpenfireAdmin(settings) if settings.openfire_enabled else None
        if openfire is None:
            logger.info("Openfire admin tools disabled (OPENFIRE_* not configured)")

        try:
            yield {
                CTX_XMPP: xmpp,
                CTX_SETTINGS: settings,
                CTX_OPENFIRE: openfire,
                CTX_CHANNEL: bridge,
            }
        finally:
            if bridge is not None:
                await bridge.aclose()
            await xmpp.stop()
            if openfire is not None:
                await openfire.aclose()

    return _lifespan


def create_server(**overrides: Any) -> FastMCP:
    """Construct the FastMCP app with all tools and resources registered.

    Settings are loaded here (environment / ``.env``, with ``overrides`` —
    e.g. from command-line flags — taking precedence) because channel mode
    changes what the server declares at ``initialize``: the
    ``claude/channel`` capability and channel-specific instructions.
    """
    settings = load_settings(**overrides)
    instructions = _BASE_INSTRUCTIONS
    experimental: dict[str, dict[str, Any]] = {}
    if settings.xmpp_channel:
        experimental[CHANNEL_CAPABILITY] = {}
        instructions = (
            channel_instructions(settings.xmpp_jid, settings.display_name)
            + "\n\n" + _BASE_INSTRUCTIONS
        )

    mcp: FastMCP = FastMCP(
        "xmpp-mcp",
        instructions=instructions,
        lifespan=_make_lifespan(settings),
        experimental_capabilities=experimental or None,
    )
    if settings.xmpp_channel:
        mcp.add_middleware(ChannelSessionMiddleware(CTX_CHANNEL))

    messaging.register(mcp)
    agents.register(mcp)
    presence.register(mcp)
    muc.register(mcp)
    disco.register(mcp)
    pubsub.register(mcp)
    mam.register(mcp)
    admin.register(mcp)

    return mcp


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xmpp-mcp",
        description=(
            "MCP server for XMPP. Every option can also be set through the "
            "environment variable shown; flags win over the environment."
        ),
    )
    parser.add_argument(
        "--channel", action="store_true", default=None,
        help="push inbound messages to Claude Code via the claude/channel "
             "capability (XMPP_CHANNEL)",
    )
    parser.add_argument(
        "--agent-name", metavar="NAME",
        help="this agent's name; fills {agent} in the JID template (XMPP_AGENT_NAME)",
    )
    parser.add_argument(
        "--jid", metavar="JID",
        help="account JID or template, e.g. '{agent}.{host}@example.com' (XMPP_JID)",
    )
    parser.add_argument(
        "--join", metavar="ROOM", action="append",
        help="MUC room to join at startup; repeatable (XMPP_AUTO_JOIN)",
    )
    parser.add_argument(
        "--allow", metavar="PATTERN", action="append",
        help="sender pattern allowed through the channel; repeatable (XMPP_CHANNEL_ALLOW)",
    )
    parser.add_argument(
        "--register", action="store_true", default=None,
        help="create the account via XEP-0077 in-band registration if missing (XMPP_REGISTER)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Console-script entry point.

    Default transport is **stdio** (the MCP norm — Claude Desktop / Claude Code
    spawn the binary as a subprocess and talk JSON-RPC to it). Set
    ``XMPP_MCP_TRANSPORT=http`` to run a long-lived HTTP server instead, useful
    for poking at the tool surface from a browser, curl, or a remote client.

    HTTP knobs:
      * ``XMPP_MCP_HTTP_HOST`` (default 127.0.0.1)
      * ``XMPP_MCP_HTTP_PORT`` (default 8765)
    """
    args = _parse_args(argv)
    transport = os.environ.get("XMPP_MCP_TRANSPORT", "stdio").lower()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout is reserved for the MCP stdio transport
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = create_server(  # noqa: F841 - assigned below for clarity
        xmpp_channel=args.channel,
        xmpp_agent_name=args.agent_name,
        xmpp_jid=args.jid,
        xmpp_auto_join=",".join(args.join) if args.join else None,
        xmpp_channel_allow=",".join(args.allow) if args.allow else None,
        xmpp_register=args.register,
    )
    try:
        if transport == "http":
            if os.environ.get("XMPP_CHANNEL", "").lower() in ("1", "true", "yes") or args.channel:
                # Channel pushes ride the connection's standalone notification
                # channel, which only exists once a client has completed the
                # initialize handshake. FastMCP's HTTP transport answers a
                # modern client without one, so nothing would ever be
                # delivered. Claude Code spawns MCP servers over stdio.
                logger.warning(
                    "Channel mode is only delivered over the stdio transport; "
                    "inbound messages will queue and be dropped under HTTP"
                )
            host = os.environ.get("XMPP_MCP_HTTP_HOST", "127.0.0.1")
            port = int(os.environ.get("XMPP_MCP_HTTP_PORT", "8765"))
            logger.info("Running MCP server on http://%s:%s/mcp", host, port)
            server.run(transport="http", host=host, port=port, show_banner=False)
        else:
            # show_banner=False: the stdio transport reserves stdout for the MCP
            # protocol — a banner printed there would corrupt the stream.
            server.run(show_banner=False)
    except KeyboardInterrupt:
        # Ctrl-C when run by hand: the lifespan already tore the XMPP session
        # down cleanly, so exit quietly instead of dumping an anyio/asyncio
        # cancellation traceback.
        logger.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
