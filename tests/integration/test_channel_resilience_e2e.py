"""Channel resilience: the agent survives an XMPP server restart.

Unlike test_resilience_e2e.py (docker pause/unpause, TCP preserved), this
restarts the ejabberd container, so the stream really drops. The agent must
reconnect on its own (capped backoff), re-announce presence and re-join its
rooms, and keep pushing channel events — without the MCP session noticing.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from .conftest import EjabberdHandle, _wait_for_ejabberd

pytestmark = [pytest.mark.docker, pytest.mark.ejabberd]


async def _retry(coro_factory, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await coro_factory()
        except Exception:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1.0)


async def test_agent_reconnects_and_rejoins_after_server_restart(
    spawn_agent, ejabberd: EjabberdHandle
) -> None:
    room = ejabberd.room_jid(f"resil-{uuid.uuid4().hex[:8]}")
    agent = await spawn_agent("phoenix", "--join", room)

    await asyncio.to_thread(
        subprocess.run, ["docker", "restart", ejabberd.container],
        check=True, capture_output=True,
    )
    await asyncio.to_thread(_wait_for_ejabberd, ejabberd)

    async def dm_round_trip() -> dict:
        async with ejabberd.raw("alice") as alice:
            alice.send_chat(agent.jid, "are you back?")
            return await agent.next_event(timeout=5)

    # Reconnect backoff is 1s doubling to 30s; allow a couple of cycles.
    ev = await _retry(dm_round_trip, timeout=60)
    assert ev["content"] == "are you back?"

    # Room occupancy was restored too (XEP-0045: it never survives a stream).
    identity = await agent.call("get_identity")
    assert identity["rooms"] == [{"room": room, "nick": agent.agent_name}]
    async with ejabberd.raw("alice") as alice:
        await alice.join_muc(room, "alice")
        alice.send_groupchat(room, "room still works")
        ev = await agent.next_event(timeout=10)
    assert (ev["content"], ev["meta"]["room"]) == ("room still works", room)
