"""Unit tests for per-agent JID templating (xmpp_mcp.identity)."""

from __future__ import annotations

import pytest

from xmpp_mcp.identity import IdentityError, expand_template, normalise, template_fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("myagent", "myagent"),
        ("My Agent", "my-agent"),
        ("Reviewer #2!", "reviewer-2"),
        ("  code/review@team  ", "code-review-team"),  # RFC 7622 forbidden chars
        ("a\"b&c'd:e<f>g", "a-b-c-d-e-f-g"),
        ("host1.foo", "host1.foo"),  # dots survive (valid in localpart and DNS)
        ("--edge--", "edge"),
        # "_" is legal in a localpart but not in a DNS label, and a template
        # may place the value in the domain — so it folds like anything else.
        ("x___y", "x-y"),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "@@@", "---"])
def test_normalise_rejects_empty_result(raw: str) -> None:
    with pytest.raises(IdentityError):
        normalise(raw)


def test_template_fields() -> None:
    assert template_fields("{agent}.{host}@x.test") == {"agent", "host"}
    assert template_fields("bot@x.test") == set()


@pytest.mark.parametrize("bad", ["{agnet}@x.test", "{agent!r}@x", "{agent:>5}@x", "{agent@x"])
def test_template_fields_rejects_unknown_or_formatted(bad: str) -> None:
    with pytest.raises(IdentityError):
        template_fields(bad)


def test_plain_jid_passes_through_unchanged() -> None:
    # No placeholders: not even lowercased — this is someone's literal JID.
    assert expand_template("Bot@Example.com") == "Bot@Example.com"


def test_expand_agent_at_host() -> None:
    # The motivating example: myagent@host1.foo
    assert expand_template("{agent}@{host}", agent="myagent", host="host1.foo") == "myagent@host1.foo"


def test_expand_normalises_values() -> None:
    jid = expand_template("{agent}.{host}@xmpp.test", agent="Code Reviewer", host="Build-Box")
    assert jid == "code-reviewer.build-box@xmpp.test"


def test_expand_in_resource() -> None:
    jid = expand_template("agents@xmpp.test/{agent}", agent="alpha")
    assert jid == "agents@xmpp.test/alpha"


def test_expand_uses_machine_hostname_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "Worker7.cluster.local")
    assert expand_template("{agent}.{host}@x.test", agent="a") == "a.worker7@x.test"


def test_expand_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.getfqdn", lambda: "worker7.cluster.local")
    assert expand_template("{agent}@{fqdn}", agent="a") == "a@worker7.cluster.local"


def test_expand_requires_agent_name() -> None:
    with pytest.raises(IdentityError, match="XMPP_AGENT_NAME"):
        expand_template("{agent}@x.test")
