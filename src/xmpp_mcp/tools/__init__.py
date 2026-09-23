"""MCP tool modules. Each submodule exposes a ``register(mcp)`` function.

This package also holds the lifespan-context keys and small accessor helpers so
tool modules and ``server.py`` agree on how shared state is reached.
"""

from __future__ import annotations

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..config import Settings
from ..openfire_admin import OpenfireAdmin
from ..xmpp_client import XMPPClient

# Keys used inside ctx.lifespan_context (see server._lifespan).
CTX_XMPP = "xmpp"
CTX_SETTINGS = "settings"
CTX_OPENFIRE = "openfire"
CTX_CHANNEL = "channel"  # ChannelBridge in channel mode, else None


def get_xmpp(ctx: Context) -> XMPPClient:
    """Return the live XMPP client from the lifespan context."""
    return ctx.lifespan_context[CTX_XMPP]


def get_settings(ctx: Context) -> Settings:
    """Return the loaded Settings from the lifespan context."""
    return ctx.lifespan_context[CTX_SETTINGS]


def get_openfire(ctx: Context) -> OpenfireAdmin:
    """Return the Openfire admin client, or raise a clean error if not configured."""
    openfire = ctx.lifespan_context.get(CTX_OPENFIRE)
    if openfire is None:
        raise ToolError(
            "Openfire admin tools are disabled. Set OPENFIRE_BASE_URL and either "
            "OPENFIRE_SECRET_KEY or OPENFIRE_ADMIN_USER/OPENFIRE_ADMIN_PASSWORD."
        )
    return openfire
