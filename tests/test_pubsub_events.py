"""Unit tests for the pubsub-event buffer on :class:`XMPPClient`.

No network: events are appended via ``_record_event`` directly and the public
``drain_pubsub_events`` method is exercised in isolation.
"""

from __future__ import annotations

import pytest

from xmpp_mcp.config import Settings
from xmpp_mcp.xmpp_client import XMPPClient


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        xmpp_jid="bot@xmpp.test",
        xmpp_password="x",
        xmpp_inbox_size=20,
    )


async def _client_with_events(events: list[dict]) -> XMPPClient:
    c = XMPPClient(_settings())
    for ev in events:
        c._record_event(dict(ev))
    return c


_EVENTS = [
    {"kind": "publish", "service": "pubsub.xmpp.test", "node": "alpha", "item_id": "a1"},
    {"kind": "publish", "service": "pubsub.xmpp.test", "node": "beta",  "item_id": "b1"},
    {"kind": "retract", "service": "pubsub.xmpp.test", "node": "alpha", "item_id": "a1"},
    {"kind": "publish", "service": "pubsub.xmpp.test", "node": "alpha", "item_id": "a2"},
    {"kind": "config",  "service": "pubsub.xmpp.test", "node": "beta"},
]


async def test_empty_buffer_returns_empty() -> None:
    c = await _client_with_events([])
    assert c.drain_pubsub_events() == []


async def test_drain_returns_every_event_in_order() -> None:
    c = await _client_with_events(_EVENTS)
    got = c.drain_pubsub_events()
    assert [e["kind"] for e in got] == ["publish", "publish", "retract", "publish", "config"]
    assert [e.get("node") for e in got] == ["alpha", "beta", "alpha", "alpha", "beta"]


async def test_filter_by_kind_leaves_others_in_buffer() -> None:
    c = await _client_with_events(_EVENTS)
    publishes = c.drain_pubsub_events(kind="publish")
    assert [e["item_id"] for e in publishes] == ["a1", "b1", "a2"]
    # The retract and config events are still there for a follow-up drain.
    remaining = c.drain_pubsub_events()
    assert [e["kind"] for e in remaining] == ["retract", "config"]


async def test_filter_by_node() -> None:
    c = await _client_with_events(_EVENTS)
    on_beta = c.drain_pubsub_events(node="beta")
    assert [e["kind"] for e in on_beta] == ["publish", "config"]


async def test_combined_node_and_kind_filter() -> None:
    c = await _client_with_events(_EVENTS)
    got = c.drain_pubsub_events(node="alpha", kind="publish")
    assert [e["item_id"] for e in got] == ["a1", "a2"]


async def test_limit_caps_matches_and_keeps_rest() -> None:
    c = await _client_with_events(_EVENTS)
    first = c.drain_pubsub_events(kind="publish", limit=1)
    assert [e["item_id"] for e in first] == ["a1"]
    # The other two publishes remain — followed by the unmatched events.
    remaining_kinds = [e["kind"] for e in c.drain_pubsub_events()]
    assert remaining_kinds == ["publish", "retract", "publish", "config"]


async def test_record_adds_timestamp() -> None:
    c = await _client_with_events([])
    c._record_event({"kind": "publish", "service": "s", "node": "n"})
    got = c.drain_pubsub_events()
    assert "timestamp" in got[0]


# --- the handler must not rewind slixmpp's own dispatch loop -----------------


def _event(c: XMPPClient, with_id: bool, items: int = 1):
    from slixmpp import Message
    from slixmpp.xmlstream import ET

    body = "".join(
        f'<item id="i{n}"><nick xmlns="http://jabber.org/protocol/nick">n{n}</nick></item>'
        for n in range(items)
    )
    id_attr = ' id="m1"' if with_id else ""
    raw = (
        f'<message xmlns="jabber:client" from="bot@xmpp.test" to="bot@xmpp.test/r"{id_attr}>'
        '<event xmlns="http://jabber.org/protocol/pubsub#event">'
        f'<items node="http://jabber.org/protocol/nick">{body}</items></event></message>'
    )
    return Message(c.xmpp, ET.fromstring(raw))


@pytest.mark.parametrize("with_id", [True, False])
@pytest.mark.parametrize("items", [1, 3])
async def test_notification_dispatch_terminates(with_id: bool, items: int) -> None:
    """Regression: an id-less notification once hung the event loop for ever.

    slixmpp stanzas are their own iterators. xep_0060 loops over the items
    and fires our handler synchronously for each; if the handler iterates the
    same stanza it rewinds that loop. Openfire's notifications carry an id,
    which let a dedupe paper over it; ejabberd's PEP notifications do not.
    """
    c = XMPPClient(_settings())
    fires = 0
    handler = c._on_pubsub_items

    def counting(msg):
        nonlocal fires
        fires += 1
        if fires > 50:
            raise AssertionError("dispatch loop is not terminating")
        handler(msg)

    c.xmpp.del_event_handler("pubsub_publish", handler)
    c.xmpp.add_event_handler("pubsub_publish", counting)
    c.xmpp.plugin["xep_0060"]._handle_event_items(_event(c, with_id, items))
    assert fires == items  # one fire per item, as slixmpp intends
    got = c.drain_pubsub_events()
    assert [e["item_id"] for e in got] == [f"i{n}" for n in range(items)]  # each once
