"""Unit tests for the relay's routing: explicit, envelope, route table, defaults.

Also covers per-provider de-duplication and friendly-name targets — no
network; the relay's XMPP client is built but never connected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xmpp_mcp.webhook_relay import (
    RelaySettings, Route, RouteError, RoutingError, Target, WebhookRelay, load_routes,
    parse_routes, resolve_targets, strip_envelope,
)

AGENT = "rev.host1@xmpp.test"
ROOM = "agents@conference.xmpp.test"
PR = {"action": "opened", "repository": {"full_name": "abligh/xmpp-mcp"},
      "pull_request": {"number": 7, "title": "t", "html_url": "u"}}


def _route(**kw: Any) -> Route:
    return parse_routes({"route": [kw]})[0]


def _resolve(**kw: Any):
    base: dict[str, Any] = {"path": "/", "query": {}, "headers": {}}
    return resolve_targets(**{**base, **kw})


# --- precedence --------------------------------------------------------------


def test_explicit_beats_everything() -> None:
    r = _resolve(path=f"/agent/{AGENT}", payload={"xmpp": {"room": ROOM}},
                 routes=[_route(room=ROOM)], default_room=ROOM)
    assert (r.how, r.targets) == ("explicit", (Target("agent", AGENT),))


def test_envelope_beats_routes_and_defaults() -> None:
    r = _resolve(payload={"xmpp": {"to": AGENT}}, routes=[_route(room=ROOM)], default_room=ROOM)
    assert (r.how, r.targets) == ("envelope", (Target("agent", AGENT),))


def test_envelope_can_be_disabled() -> None:
    r = _resolve(payload={"xmpp": {"to": AGENT}}, envelope=False, default_room=ROOM)
    assert r.how == "default"


def test_routes_beat_defaults() -> None:
    r = _resolve(payload=PR, provider="github", event="pull_request",
                 routes=[_route(provider="github", event="pull_request", room=ROOM)],
                 default_to=AGENT)
    assert (r.how, r.targets) == ("routes", (Target("room", ROOM),))


def test_nothing_matches() -> None:
    with pytest.raises(RoutingError, match="no target"):
        _resolve(payload={"a": 1}, routes=[_route(provider="github", room=ROOM)])


# --- the envelope ------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [{"to": AGENT, "room": ROOM}, {}, "a string", {"room": "not-a-room"}, {"to": ""}],
)
def test_bad_envelopes(env: Any) -> None:
    with pytest.raises(RoutingError):
        _resolve(payload={"xmpp": env})


def test_envelope_is_stripped_before_forwarding() -> None:
    assert strip_envelope({"xmpp": {"to": AGENT}, "event": "x"}) == {"event": "x"}
    assert strip_envelope(["not", "a", "dict"]) == ["not", "a", "dict"]


# --- the route table ---------------------------------------------------------


def test_every_matching_route_delivers_and_duplicates_collapse() -> None:
    routes = [
        _route(name="to-agent", provider="github", to=AGENT),
        _route(name="to-room", match={"repository.full_name": "abligh/*"}, room=ROOM),
        _route(name="again", provider="github", to=AGENT),  # same target twice
        _route(name="gitlab-only", provider="gitlab", room="other@conference.xmpp.test"),
    ]
    r = _resolve(payload=PR, provider="github", event="pull_request", routes=routes)
    assert r.targets == (Target("agent", AGENT), Target("room", ROOM))
    assert r.routes == ("to-agent", "to-room", "again")


@pytest.mark.parametrize(
    ("match", "hit"),
    [
        ({"action": "opened"}, True),
        ({"action": "op*"}, True),                                   # glob
        ({"action": ["closed", "opened"]}, True),                    # any of
        ({"action": "closed"}, False),
        ({"repository.full_name": "abligh/xmpp-mcp"}, True),         # dotted path
        ({"repository.missing": "*"}, False),                        # missing never matches
        ({"repository": "*"}, False),                                # a glob never matches a dict
        ({"pull_request.number": 7}, True),                          # typed equality
        ({"pull_request.number": "7"}, True),                        # glob over the text
        ({"pull_request.number": 7.0}, False),                       # not the same type
    ],
)
def test_match_semantics(match: dict[str, Any], hit: bool) -> None:
    assert _route(room=ROOM, match=match).matches("github", "pull_request", "/", PR) is hit


def test_event_and_path_globs() -> None:
    r = _route(room=ROOM, event="pull_request*", path="/hooks/*")
    assert r.matches("github", "pull_request_review", "/hooks/gh", PR)
    assert not r.matches("github", "push", "/hooks/gh", PR)
    assert not r.matches("github", "pull_request", "/elsewhere", PR)


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"to": AGENT, "room": ROOM}, "exactly one"),
        ({}, "exactly one"),
        ({"to": AGENT, "typo": 1}, "unknown keys"),
        ({"to": AGENT, "provider": "bitbucket"}, "unknown provider"),
        ({"to": AGENT, "match": "nope"}, "must be a table"),
        ({"to": 7}, "must be a string"),
    ],
)
def test_route_table_validation(entry: dict[str, Any], error: str) -> None:
    with pytest.raises(RouteError, match=error):
        parse_routes({"route": [entry]})


def test_route_table_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "routes.toml"
    path.write_text('''
[[route]]
name = "prs-to-reviewer"
provider = "github"
event = "pull_request"
match = { "repository.full_name" = "abligh/xmpp-mcp", action = "opened" }
to = "Reviewer"

[[route]]
match = { "workflow_run.conclusion" = ["failure", "timed_out"] }
room = "agents@conference.xmpp.test"
''')
    routes = load_routes(path)
    assert [r.name for r in routes] == ["prs-to-reviewer", "#2"]
    assert routes[0].to == "Reviewer"


def test_route_table_errors_name_the_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[[route]\n")
    with pytest.raises(RouteError, match="bad.toml"):
        load_routes(path)
    with pytest.raises(RouteError, match="cannot read"):
        load_routes(tmp_path / "missing.toml")


# --- friendly-name targets ---------------------------------------------------


def test_a_target_without_an_at_is_a_name() -> None:
    r = _resolve(path="/agent/Reviewer")
    assert r.targets[0] == Target("agent", "Reviewer") and r.targets[0].is_name
    assert not Target("agent", AGENT).is_name
    with pytest.raises(RoutingError):
        _resolve(path="/room/Reviewer")  # rooms are always JIDs


def _relay(**kw: Any) -> WebhookRelay:
    base: dict[str, Any] = {"xmpp_jid": "webhook@xmpp.test", "xmpp_password": "x"}
    return WebhookRelay(RelaySettings(_env_file=None, **{**base, **kw}))  # type: ignore[arg-type]


def _occupant(relay: WebhookRelay, nick: str, name: str, agent_id: str, real: str) -> None:
    pres = relay.xmpp.make_presence(pfrom=f"{ROOM}/{nick}", pto="webhook@xmpp.test/r")
    from xml.etree import ElementTree as ET
    ET.SubElement(pres.xml, "{urn:xmpp-mcp:agent:0}agent", {"id": agent_id, "name": name})
    x = ET.SubElement(pres.xml, "{http://jabber.org/protocol/muc#user}x")
    ET.SubElement(x, "{http://jabber.org/protocol/muc#user}item", {"jid": real})
    relay.directory.update(pres)


def test_names_resolve_through_the_directory_room() -> None:
    relay = _relay(directory_room=ROOM)
    _occupant(relay, "Reviewer", "Reviewer", "sess-r", "sess-r@xmpp.test/x")
    _occupant(relay, "Builder", "Builder", "sess-b", "sess-b@xmpp.test/y")
    assert relay.resolve_name("reviewer") == "sess-r@xmpp.test"
    assert relay.resolve_name("sess-b") == "sess-b@xmpp.test"
    with pytest.raises(RuntimeError, match="no agent"):
        relay.resolve_name("Nobody")


def test_ambiguous_names_are_not_guessed() -> None:
    relay = _relay(directory_room=ROOM)
    _occupant(relay, "Reviewer", "Reviewer", "a", "a@xmpp.test/x")
    _occupant(relay, "Reviewer (host2)", "Reviewer", "b", "b@xmpp.test/y")
    with pytest.raises(RuntimeError, match="ambiguous"):
        relay.resolve_name("Reviewer")


def test_names_need_a_directory_room() -> None:
    with pytest.raises(RuntimeError, match="WEBHOOK_DIRECTORY_ROOM"):
        _relay().resolve_name("Reviewer")


async def test_a_resolved_name_is_still_subject_to_the_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xmpp_mcp.webhook_relay import Outgoing

    relay = _relay(directory_room=ROOM, allowed_targets="*@trusted.test")
    _occupant(relay, "Reviewer", "Reviewer", "sess-r", "sess-r@xmpp.test/x")
    monkeypatch.setattr(relay.xmpp, "send", lambda stanza: None)
    with pytest.raises(RuntimeError, match="not allowed"):
        await relay._deliver(Outgoing(Target("agent", "Reviewer"), "b", "id"))


# --- over HTTP ---------------------------------------------------------------


async def _client(relay: WebhookRelay) -> TestClient:
    client = TestClient(TestServer(relay.make_app()))
    await client.start_server()
    return client


async def test_http_route_table_fans_out(tmp_path: Path) -> None:
    routes = tmp_path / "routes.toml"
    routes.write_text(f'''
[[route]]
provider = "github"
event = "pull_request"
to = "{AGENT}"

[[route]]
match = {{ "repository.full_name" = "abligh/*" }}
room = "{ROOM}"
''')
    relay = _relay(routes=str(routes), github_secret="s")
    client = await _client(relay)
    try:
        body = json.dumps(PR).encode()
        sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
        resp = await client.post("/", data=body, headers={
            "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        got = await resp.json()
    finally:
        await client.close()
    assert got["routed_by"] == "routes" and got["routes"] == ["#1", "#2"]
    assert got["targets"] == [{"to": AGENT, "kind": "agent"}, {"to": ROOM, "kind": "room"}]
    ids = [relay.queue.get_nowait().msg_id for _ in range(2)]
    assert ids == ["d-1-1", "d-1-2"]  # distinct stanza ids, shared delivery


async def test_http_envelope_is_honoured_and_stripped() -> None:
    relay = _relay()
    client = await _client(relay)
    try:
        resp = await client.post("/", json={"xmpp": {"room": ROOM}, "deploy": "done"})
        assert (await resp.json())["routed_by"] == "envelope"
    finally:
        await client.close()
    item = relay.queue.get_nowait()
    assert item.target == Target("room", ROOM)
    assert '"xmpp"' not in item.body and json.loads(item.payload_json) == {"deploy": "done"}


async def test_route_table_is_validated_at_startup(tmp_path: Path) -> None:
    bad = tmp_path / "routes.toml"
    bad.write_text('[[route]]\nto = "a@b"\nroom = "c@d"\n')
    with pytest.raises(RouteError):
        _relay(routes=str(bad))


@pytest.mark.parametrize(
    "headers",
    [
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "same"},
        {"X-Gitlab-Event": "Push Hook", "X-Gitlab-Event-UUID": "same"},
        {"X-Webhook-Delivery": "same"},
    ],
)
async def test_every_provider_deduplicates_by_its_own_delivery_id(headers: dict[str, str]) -> None:
    """Regression: de-duplication once read X-GitHub-Delivery for every sender."""
    relay = _relay()
    client = await _client(relay)
    try:
        first = await client.post(f"/agent/{AGENT}", json={"a": 1}, headers=headers)
        again = await client.post(f"/agent/{AGENT}", json={"a": 1}, headers=headers)
        assert (await first.json())["status"] == "queued"
        assert (await again.json())["status"] == "duplicate"
    finally:
        await client.close()
    assert relay.queue.qsize() == 1


# --- names checked when the caller gives them ---------------------------------


def _ready(relay: WebhookRelay) -> None:
    """As if the relay were online and had joined its directory room."""
    relay.online.set()
    relay._rooms.add(ROOM)


async def _post(relay: WebhookRelay, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
    client = await _client(relay)
    try:
        resp = await client.post(path, **kw)
        return resp.status, await resp.json()
    finally:
        await client.close()


async def test_an_unknown_name_is_refused_not_queued() -> None:
    relay = _relay(directory_room=ROOM)
    _ready(relay)
    status, got = await _post(relay, "/agent/Nobody", data="wake up")
    assert status == 404 and "no agent named 'Nobody'" in got["error"]
    assert relay.queue.empty()


async def test_an_ambiguous_name_is_a_conflict() -> None:
    relay = _relay(directory_room=ROOM)
    _ready(relay)
    _occupant(relay, "Reviewer", "Reviewer", "a", "a@xmpp.test/x")
    _occupant(relay, "Reviewer (host2)", "Reviewer", "b", "b@xmpp.test/y")
    status, _ = await _post(relay, "/agent/Reviewer", data="x")
    assert status == 409 and relay.queue.empty()


async def test_before_the_directory_loads_the_answer_is_retry_not_absent() -> None:
    """A 404 here would tell a caller to give up on an agent that exists."""
    relay = _relay(directory_room=ROOM)
    relay.online.set()  # connected, but the room isn't joined yet
    client = await _client(relay)
    try:
        resp = await client.post("/agent/Reviewer", data="x")
        assert resp.status == 503 and resp.headers["Retry-After"]
    finally:
        await client.close()
    assert relay.queue.empty()


async def test_a_name_needs_a_directory_room_up_front() -> None:
    status, got = await _post(_relay(), "/agent/Reviewer", data="x")
    assert status == 400 and "WEBHOOK_DIRECTORY_ROOM" in got["error"]


async def test_a_known_name_is_queued_as_a_name_and_resolved_again_later() -> None:
    relay = _relay(directory_room=ROOM, allowed_targets="*@xmpp.test")
    _ready(relay)
    _occupant(relay, "Reviewer", "Reviewer", "sess-r", "sess-r@xmpp.test/x")
    status, got = await _post(relay, "/agent/Reviewer", data="x")
    assert status == 200 and got["status"] == "queued"
    # Still the name: whoever holds it at delivery gets it.
    assert relay.queue.get_nowait().target == Target("agent", "Reviewer")


async def test_a_name_resolving_outside_the_allow_list_is_refused_up_front() -> None:
    relay = _relay(directory_room=ROOM, allowed_targets="*@trusted.test")
    _ready(relay)
    _occupant(relay, "Reviewer", "Reviewer", "sess-r", "sess-r@xmpp.test/x")
    status, got = await _post(relay, "/agent/Reviewer", data="x")
    assert status == 403 and "not allowed" in got["error"]


async def test_names_from_the_route_table_still_fail_at_delivery(tmp_path: Path) -> None:
    """One stale rule mustn't stop the event reaching its other targets."""
    routes = tmp_path / "routes.toml"
    routes.write_text(f'[[route]]\nto = "Nobody"\n\n[[route]]\nroom = "{ROOM}"\n')
    relay = _relay(routes=str(routes), directory_room=ROOM)
    _ready(relay)
    status, got = await _post(relay, "/", data="x")
    assert status == 200 and len(got["targets"]) == 2


# --- verbatim bodies -----------------------------------------------------------


async def test_a_verbatim_body_is_the_whole_message() -> None:
    relay = _relay()
    text = 'From: nightly-wake\n{"looks": "like json"}\n'
    status, _ = await _post(relay, f"/agent/{AGENT}", data=text, headers={
        "Content-Type": "text/plain", "X-XMPP-Verbatim": "1"})
    assert status == 200
    item = relay.queue.get_nowait()
    assert item.body == text  # no summary line, no blank line
    assert item.payload_json is None  # and no <json/> container


async def test_verbatim_is_opt_in() -> None:
    relay = _relay()
    await _post(relay, f"/agent/{AGENT}", data="From: x",
                headers={"Content-Type": "text/plain"})
    assert relay.queue.get_nowait().body.endswith("\n\nFrom: x")


async def test_a_verbatim_body_can_not_carry_an_envelope() -> None:
    relay = _relay()
    status, _ = await _post(relay, "/", data='{"xmpp": {"to": "' + AGENT + '"}}', headers={
        "Content-Type": "text/plain", "X-XMPP-Verbatim": "1"})
    assert status == 400  # no target: the text was not read as JSON


@pytest.mark.parametrize("headers,data", [
    ({"Content-Type": "application/json", "X-XMPP-Verbatim": "1"}, '{"a": 1}'),
    ({"Content-Type": "text/plain", "X-XMPP-Verbatim": "1"}, "  \n"),
])
async def test_verbatim_refuses_what_it_cannot_send_as_is(
        headers: dict[str, str], data: str) -> None:
    relay = _relay()
    status, _ = await _post(relay, f"/agent/{AGENT}", data=data, headers=headers)
    assert status == 400 and relay.queue.empty()


async def test_a_verbatim_body_is_still_scrubbed_and_limited() -> None:
    relay = _relay(max_message_bytes=256)
    await _post(relay, f"/agent/{AGENT}", data="a\x0cb" + "x" * 400, headers={
        "Content-Type": "text/plain", "X-XMPP-Verbatim": "1"})
    body = relay.queue.get_nowait().body
    assert body.startswith("a�b") and body.endswith("…[truncated]")
