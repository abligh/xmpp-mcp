"""Webhook relay E2E: HTTP POST → relay → ejabberd → channel agent → Claude Code.

The relay runs in-process (function-scoped, on the test's event loop — see
CLAUDE.md gotcha #13) with its aiohttp app on a free port; the agent is a real
``xmpp-mcp --channel`` subprocess.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from xmpp_mcp.webhook_relay import WebhookRelay

from .conftest import LabHandle

pytestmark = [pytest.mark.docker, pytest.mark.agents]

SECRET = "s3cret"
GITLAB_TOKEN = "gl-t0ken"
PR_EVENT = {
    "action": "opened",
    "pull_request": {"number": 42, "title": "Channels over XMPP",
                     "html_url": "https://github.com/o/r/pull/42"},
    "repository": {"full_name": "o/r"},
    "sender": {"login": "octocat"},
}


@pytest_asyncio.fixture
async def relay(lab: LabHandle) -> AsyncIterator[tuple[WebhookRelay, TestServer]]:
    settings = lab.relay_settings(github_secret=SECRET, gitlab_token=GITLAB_TOKEN)
    r = WebhookRelay(settings)
    await r.start()
    await asyncio.wait_for(r.online.wait(), timeout=15)
    server = TestServer(r.make_app())
    await server.start_server()
    try:
        yield r, server
    finally:
        await server.close()
        await r.stop()


async def _post_github(server: TestServer, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as http:
        resp = await http.post(
            server.make_url(path), data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": uuid.uuid4().hex,
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status == 200, await resp.text()
        return await resp.json()


async def test_webhook_to_agent(relay, spawn_agent, lab: LabHandle) -> None:
    _relay, server = relay
    agent = await spawn_agent("hook")
    ack = await _post_github(server, f"/agent/{agent.jid}", PR_EVENT)
    assert ack["status"] == "queued"
    ev = await agent.next_event()
    first, _, rest = ev["content"].partition("\n\n")
    assert first == (
        'GitHub pull_request.opened in o/r: #42 "Channels over XMPP" '
        "https://github.com/o/r/pull/42 (by octocat)"
    )
    assert json.loads(rest) == PR_EVENT
    assert ev["meta"]["sender_jid"] == lab.relay_jid  # passes the gate


async def test_webhook_to_room_reaches_every_agent(relay, spawn_agent, lab: LabHandle) -> None:
    _relay, server = relay
    room = lab.room_jid(f"hooks-{uuid.uuid4().hex[:8]}")
    a = await spawn_agent("ra", "--join", room)
    b = await spawn_agent("rb", "--join", room)
    await _post_github(server, f"/room/{room}", PR_EVENT)
    for agent in (a, b):
        ev = await agent.next_event()
        assert ev["meta"]["type"] == "groupchat"
        assert ev["meta"]["room"] == room
        assert ev["meta"]["nick"] == "webhook"
        assert "Channels over XMPP" in ev["content"]


async def test_webhook_queued_while_xmpp_down_is_delivered_later(
    relay, spawn_agent
) -> None:
    """The HTTP side answers 200 even when XMPP is offline; delivery follows."""
    r, server = relay
    agent = await spawn_agent("late")
    r.online.clear()  # simulate "not connected": the worker parks on this event
    await _post_github(server, f"/agent/{agent.jid}", PR_EVENT)
    await agent.assert_no_event(within=1.0)
    assert r.sent == 0  # the worker holds it until the session is back
    r.online.set()
    ev = await agent.next_event()
    assert "Channels over XMPP" in ev["content"]


async def test_webhook_from_a_second_provider(relay, spawn_agent) -> None:
    """A GitLab delivery authenticates and summarises through its own provider."""
    _relay, server = relay
    agent = await spawn_agent("gl")
    payload = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "grp/proj"},
        "user_name": "Alice",
        "object_attributes": {"iid": 3, "title": "Speciate the relay", "state": "opened",
                              "url": "https://gitlab.example/grp/proj/-/merge_requests/3"},
    }
    async with aiohttp.ClientSession() as http:
        resp = await http.post(
            server.make_url(f"/agent/{agent.jid}"), json=payload,
            headers={"X-Gitlab-Event": "Merge Request Hook",
                     "X-Gitlab-Token": GITLAB_TOKEN,
                     "X-Gitlab-Event-UUID": uuid.uuid4().hex},
        )
        assert resp.status == 200, await resp.text()
    ev = await agent.next_event()
    assert ev["content"].startswith(
        'GitLab Merge Request Hook in grp/proj: #3 "Speciate the relay"'
    )


async def test_a_forged_delivery_is_rejected(relay, spawn_agent) -> None:
    """Nothing without a valid credential can put text in front of an agent."""
    _relay, server = relay
    agent = await spawn_agent("forge")
    body = json.dumps(PR_EVENT).encode()
    async with aiohttp.ClientSession() as http:
        for headers in (
            {"X-GitHub-Event": "pull_request"},                                  # unsigned
            {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=00"},  # bad sig
            {"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "wrong"},          # wrong token
            {},                                                                  # no provider
        ):
            resp = await http.post(
                server.make_url(f"/agent/{agent.jid}"), data=body,
                headers={"Content-Type": "application/json", **headers},
            )
            assert resp.status == 401, f"{headers} -> {resp.status}"
    await agent.assert_no_event(within=1.5)
