"""Unit tests for the source-agnostic relay core — no network, no XMPP server.

Routing, stanza safety, the HTTP surface and delivery accounting. Anything
that knows what GitHub or GitLab looks like is in test_webhook_providers.py.

The HTTP app is driven in-process with aiohttp's test client; the relay's
XMPP client is constructed but never connected, so deliveries stay queued
(exactly what a caller sees while the XMPP server is down).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from aiohttp.test_utils import TestClient, TestServer

from xml.etree import ElementTree as etree
from xml.sax.saxutils import escape as xml_escape

from xmpp_mcp.webhook_relay import (
    Outgoing, RelaySettings, RoutingError, Target, WebhookRelay, detect,
    format_body, resolve_target, target_allowed, xml_cost,
)

AGENT = "rev.host1@xmpp.test"
ROOM = "agents@conference.xmpp.test"

PR_EVENT = {
    "action": "opened",
    "number": 7,
    "pull_request": {"number": 7, "title": "Add channels",
                     "html_url": "https://github.com/o/r/pull/7"},
    "repository": {"full_name": "o/r"},
    "sender": {"login": "octocat"},
}


def _settings(**kw: Any) -> RelaySettings:
    base: dict[str, Any] = {"xmpp_jid": "webhook@xmpp.test", "xmpp_password": "x"}
    return RelaySettings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type]


# --- routing -----------------------------------------------------------------


def test_route_from_path() -> None:
    assert resolve_target(f"/agent/{AGENT}", {}, {}) == Target("agent", AGENT)
    assert resolve_target(f"/room/{ROOM}", {}, {}) == Target("room", ROOM)


def test_route_path_keeps_full_jid_resource() -> None:
    assert resolve_target(f"/agent/{AGENT}/laptop", {}, {}) == Target("agent", f"{AGENT}/laptop")


def test_route_from_query_then_headers_then_defaults() -> None:
    assert resolve_target("/", {"to": AGENT}, {"X-XMPP-Room": ROOM}) == Target("agent", AGENT)
    assert resolve_target("/", {}, {"X-XMPP-Room": ROOM}) == Target("room", ROOM)
    assert resolve_target("/", {}, {}, default_to=AGENT) == Target("agent", AGENT)
    assert resolve_target("/", {}, {}, default_room=ROOM) == Target("room", ROOM)


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/", {}),  # nothing names a target
        ("/", {"to": AGENT, "room": ROOM}),  # ambiguous
        ("/agent/not a jid@@", {}),
        (f"/room/{ROOM}/nick", {}),  # a room must be a bare JID
        ("/room/conference.xmpp.test", {}),  # ...with a localpart
    ],
)
def test_route_errors(path: str, query: dict[str, str]) -> None:
    with pytest.raises(RoutingError):
        resolve_target(path, query, {})


# --- formatting --------------------------------------------------------------


def test_format_body_truncates() -> None:
    body = format_body("summary", "x" * 1000, 100)
    assert xml_cost(body) <= 100
    assert body.startswith("summary\n\n") and body.endswith("[truncated]")
    assert format_body("just this", "", 100) == "just this"


@pytest.mark.parametrize(
    "raw",
    [
        "😀" * 500,      # 4 UTF-8 bytes per character
        "&" * 2000,      # 5 bytes per character once XML-escaped
        "<script>" * 300,
    ],
)
def test_format_body_budget_is_measured_in_stanza_bytes(raw: str) -> None:
    """A character-counted budget would be exceeded several times over here."""
    assert xml_cost(format_body("s", raw, 500)) <= 500


def test_format_body_honours_a_tiny_limit() -> None:
    for limit in (1, 5, 13, 20):
        assert xml_cost(format_body("summary", "payload", limit)) <= limit


def test_format_body_scrubs_xml_illegal_characters() -> None:
    """A single 0x0C in a body would abort the server's XML parser."""
    body = format_body("ok", "pwn\x0c\x1b\x08 here", 1000)
    assert "\x0c" not in body and "\x1b" not in body and "\x08" not in body
    # A lone surrogate (reachable via a "\ud800" escape in valid JSON) too.
    assert "\ud800" not in format_body("ok", "bad \ud800 char", 1000)
    etree.fromstring(f"<body>{xml_escape(body)}</body>")  # must parse


def test_summary_values_are_scrubbed_too() -> None:
    # A provider's summary interpolates payload strings, and a *validly
    # signed* delivery can legitimately carry hostile ones — so scrubbing has
    # to happen here, on the way into the stanza, not in the provider.
    payload = {"pull_request": {"number": 1, "title": "pwn\x1b\x0c", "html_url": "u"}}
    summary = detect({"X-GitHub-Event": "pull_request"}).summarise(
        payload, {"X-GitHub-Event": "pull_request"}, "/"
    )
    body = format_body(summary, "", 1000)
    assert "\x1b" not in body and "\x0c" not in body


# --- HTTP --------------------------------------------------------------------


@pytest_asyncio.fixture
async def relay_client(request: pytest.FixtureRequest) -> AsyncIterator[tuple[WebhookRelay, TestClient]]:
    kw = getattr(request, "param", {})
    relay = WebhookRelay(_settings(**kw))
    client = TestClient(TestServer(relay.make_app()))
    await client.start_server()
    try:
        yield relay, client
    finally:
        await client.close()


async def test_post_is_queued_and_answered_immediately(relay_client) -> None:
    relay, client = relay_client
    resp = await client.post(
        f"/agent/{AGENT}", json=PR_EVENT,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "abc-123"},
    )
    assert resp.status == 200  # XMPP is not even connected
    assert await resp.json() == {
        "status": "queued", "id": "abc-123", "routed_by": "explicit",
        "targets": [{"to": AGENT, "kind": "agent"}],
    }
    item: Outgoing = relay.queue.get_nowait()
    assert item.target == Target("agent", AGENT)
    assert item.body.startswith('GitHub pull_request.opened in o/r: #7 "Add channels"')
    assert json.loads(item.body.split("\n\n", 1)[1]) == PR_EVENT
    assert json.loads(item.payload_json or "") == PR_EVENT  # XEP-0335 copy


async def test_room_via_query(relay_client) -> None:
    relay, client = relay_client
    resp = await client.post(f"/?room={ROOM}", data="deploy finished")
    assert resp.status == 200
    item = relay.queue.get_nowait()
    assert item.target == Target("room", ROOM)
    assert item.body == "Webhook POST /\n\ndeploy finished"
    assert item.payload_json is None  # plain text: no JSON container


async def test_no_target_is_400(relay_client) -> None:
    _relay, client = relay_client
    resp = await client.post("/", json={})
    assert resp.status == 400
    assert "no target" in (await resp.json())["error"]


async def test_invalid_json_is_400(relay_client) -> None:
    _relay, client = relay_client
    resp = await client.post(f"/agent/{AGENT}", data="{nope",
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400


@pytest.mark.parametrize("relay_client", [{"queue_size": 1}], indirect=True)
async def test_full_queue_is_503(relay_client) -> None:
    _relay, client = relay_client
    assert (await client.post(f"/agent/{AGENT}", data="one")).status == 200
    assert (await client.post(f"/agent/{AGENT}", data="two")).status == 503


@pytest.mark.parametrize("relay_client", [{"token": "t0ken"}], indirect=True)
async def test_token_auth(relay_client) -> None:
    _relay, client = relay_client
    assert (await client.post(f"/agent/{AGENT}", data="x")).status == 401
    ok_bearer = await client.post(f"/agent/{AGENT}", data="x",
                                  headers={"Authorization": "Bearer t0ken"})
    ok_header = await client.post(f"/agent/{AGENT}", data="x",
                                  headers={"X-Webhook-Token": "t0ken"})
    assert ok_bearer.status == ok_header.status == 200


@pytest.mark.parametrize("relay_client", [{"github_secret": "s3cret"}], indirect=True)
async def test_github_hmac_auth(relay_client) -> None:
    _relay, client = relay_client
    body = json.dumps(PR_EVENT).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    ok = await client.post(f"/agent/{AGENT}", data=body,
                           headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert ok.status == 200
    bad = await client.post(f"/agent/{AGENT}", data=body,
                            headers={"X-Hub-Signature-256": "sha256=00"})
    assert bad.status == 401


@pytest.mark.parametrize("relay_client", [{"max_message_bytes": 300}], indirect=True)
async def test_oversized_payload_truncated_without_container(relay_client) -> None:
    relay, client = relay_client
    await client.post(f"/agent/{AGENT}", json={"blob": "x" * 5000})
    item = relay.queue.get_nowait()
    assert xml_cost(item.body) <= 300 and item.body.endswith("[truncated]")
    assert item.payload_json is None


async def test_health(relay_client) -> None:
    _relay, client = relay_client
    resp = await client.get("/healthz")
    assert await resp.json() == {
        "xmpp": "offline", "jid": None, "queued": 0, "sent": 0, "failed": 0,
        "duplicates": 0, "providers": ["github", "gitlab", "generic"],
        "authenticated": False,
    }


# --- delivery ----------------------------------------------------------------


async def test_deliver_builds_the_right_stanzas(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = WebhookRelay(_settings(message_type="normal"))
    sent: list[Any] = []
    joined: list[str] = []
    monkeypatch.setattr(relay.xmpp, "send", sent.append)

    async def fake_join(room: str) -> None:
        joined.append(room)

    monkeypatch.setattr(relay, "_ensure_joined", fake_join)

    await relay._deliver(Outgoing(Target("agent", AGENT), "hello", "id-1", '{"a":1}'))
    await relay._deliver(Outgoing(Target("room", ROOM), "to all", "id-2"))

    dm, groupchat = sent
    assert (dm["to"], dm["type"], dm["body"], dm["id"]) == (AGENT, "normal", "hello", "id-1")
    assert dm["json"]["value"] == {"a": 1}  # XEP-0335 container
    assert (groupchat["to"], groupchat["type"]) == (ROOM, "groupchat")
    assert groupchat.xml.find("{urn:xmpp:json:0}json") is None
    assert joined == [ROOM]  # XEP-0045 §7.4: join before posting


# --- target allow-list, replay protection, hostile payloads ----------------


def test_target_allowed_patterns() -> None:
    patterns = ["*@xmpp.test", "agents@conference.xmpp.test"]
    assert target_allowed("rev.host1@xmpp.test", patterns)
    assert target_allowed("agents@conference.xmpp.test", patterns)
    assert not target_allowed("ceo@other.example", patterns)
    assert target_allowed("anyone@anywhere", [])  # unset: no restriction


@pytest.mark.parametrize(
    "relay_client", [{"allowed_targets": "*@xmpp.test"}], indirect=True
)
async def test_disallowed_target_is_403(relay_client) -> None:
    relay, client = relay_client
    assert (await client.post(f"/agent/{AGENT}", data="x")).status == 200
    resp = await client.post("/agent/ceo@other.example", data="x")
    assert resp.status == 403
    assert relay.queue.qsize() == 1  # only the allowed one was queued


async def test_repeated_delivery_id_is_dropped(relay_client) -> None:
    """A captured GitHub delivery must not be replayable at another JID."""
    relay, client = relay_client
    headers = {"X-GitHub-Delivery": "delivery-1", "Content-Type": "application/json"}
    first = await client.post(f"/agent/{AGENT}", json=PR_EVENT, headers=headers)
    assert (await first.json())["status"] == "queued"
    replay = await client.post(f"/room/{ROOM}", json=PR_EVENT, headers=headers)
    assert (await replay.json())["status"] == "duplicate"
    assert relay.queue.qsize() == 1
    assert relay.duplicates == 1


@pytest.mark.parametrize("relay_client", [{"dedupe_size": 0}], indirect=True)
async def test_dedupe_can_be_disabled(relay_client) -> None:
    relay, client = relay_client
    headers = {"X-GitHub-Delivery": "d-1"}
    await client.post(f"/agent/{AGENT}", data="a", headers=headers)
    await client.post(f"/agent/{AGENT}", data="b", headers=headers)
    assert relay.queue.qsize() == 2


async def test_control_characters_never_reach_a_stanza(relay_client) -> None:
    relay, client = relay_client
    await client.post(f"/agent/{AGENT}", data=b"pwn\x0c\x1b\x08 here")
    item = relay.queue.get_nowait()
    etree.fromstring(f"<body>{xml_escape(item.body)}</body>")  # must parse


@pytest.mark.parametrize("bad", ["\x80token", "tök"])
async def test_non_ascii_auth_headers_are_401_not_500(bad: str) -> None:
    relay = WebhookRelay(_settings(token="t0ken"))
    client = TestClient(TestServer(relay.make_app()))
    await client.start_server()
    try:
        for headers in ({"X-Webhook-Token": bad},
                        {"Authorization": f"Bearer {bad}"},
                        {"X-Hub-Signature-256": f"sha256={bad}"}):
            resp = await client.post(f"/agent/{AGENT}", data="x", headers=headers)
            assert resp.status == 401
    finally:
        await client.close()


async def test_queue_size_must_be_positive() -> None:
    """Queue(0) means *unbounded*, which is the opposite of what 0 suggests."""
    with pytest.raises(ValidationError):
        _settings(queue_size=0)
    with pytest.raises(ValidationError):
        _settings(max_message_bytes=10)


# --- delivery accounting ----------------------------------------------------


def _relay_with_flush(monkeypatch: pytest.MonkeyPatch, results: list[bool]) -> WebhookRelay:
    """A relay whose stanzas are swallowed and whose flush outcome is scripted."""
    relay = WebhookRelay(_settings())
    relay.online.set()
    relay.attempts: list[int] = []  # type: ignore[attr-defined]

    async def deliver(item: Outgoing) -> None:
        relay.attempts.append(item.attempts)  # type: ignore[attr-defined]

    async def flushed(timeout: float = 10.0) -> bool:
        return results.pop(0) if results else True

    monkeypatch.setattr(relay, "_deliver", deliver)
    monkeypatch.setattr(relay, "_flushed", flushed)
    return relay


async def test_a_dropped_stream_is_not_counted_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """slixmpp raises nothing when a dying stream eats a stanza.

    Every attempt here loses the stream, so the relay must give up and count
    a failure rather than reporting a send that never left the machine.
    """
    relay = _relay_with_flush(monkeypatch, [False, False])
    ok = await relay._deliver_with_retries(Outgoing(Target("agent", AGENT), "b", "id"), tries=2)
    assert ok is False
    assert (relay.sent, relay.failed) == (0, 1)
    assert relay.attempts == [1, 2]  # type: ignore[attr-defined]


async def test_delivery_retries_after_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _relay_with_flush(monkeypatch, [False, True])
    assert await relay._deliver_with_retries(Outgoing(Target("agent", AGENT), "b", "id"))
    # Same message id on the retry, so a peer can spot the rare duplicate.
    assert relay.attempts == [1, 2]  # type: ignore[attr-defined]
    assert (relay.sent, relay.failed) == (1, 0)


async def test_flushed_reports_a_dead_stream() -> None:
    relay = WebhookRelay(_settings())
    relay.online.set()
    assert await relay._flushed(timeout=0.1) is False  # never connected: no transport

    class Transport:
        def __init__(self) -> None:
            self.pending = 64

        def get_write_buffer_size(self) -> int:
            self.pending = max(0, self.pending - 32)
            return self.pending

    relay.xmpp.transport = Transport()  # type: ignore[assignment]
    assert await relay._flushed(timeout=1.0) is True
    relay.online.clear()
    assert await relay._flushed(timeout=0.1) is False


def _reconnect_tasks() -> list[asyncio.Task]:
    return [
        t for t in asyncio.all_tasks()
        if getattr(t.get_coro(), "__qualname__", "").startswith("WebhookRelay._reconnect")
    ]


async def test_reconnect_is_single_flight_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = WebhookRelay(_settings())
    monkeypatch.setattr(relay, "_connect", lambda: None)
    for _ in range(3):  # a flapping server fires this repeatedly
        relay._on_disconnected("boom")
    assert len(_reconnect_tasks()) == 1  # not one task (and one backoff step) per event
    await relay.stop()
    assert not [t for t in _reconnect_tasks() if not t.done()]
