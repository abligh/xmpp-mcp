"""Unit tests for the per-source webhook providers.

Each provider owns two source-specific decisions — how a sender proves who
it is, and how its payload reads as one line — plus the rule that a caller
cannot pick a weaker provider by dropping headers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from xmpp_mcp.webhook_relay import PROVIDERS, RelaySettings, detect
from xmpp_mcp.webhook_relay.auth import authorised, same_secret
from xmpp_mcp.webhook_relay.providers import GenericProvider, GitHubProvider, GitLabProvider
from xmpp_mcp.webhook_relay.providers.github import verify_signature

PR_EVENT = {
    "action": "opened",
    "pull_request": {"number": 7, "title": "Add channels",
                     "html_url": "https://github.com/o/r/pull/7"},
    "repository": {"full_name": "o/r"},
    "sender": {"login": "octocat"},
}
MR_EVENT = {
    "object_kind": "merge_request",
    "project": {"path_with_namespace": "grp/proj"},
    "user_name": "Alice",
    "object_attributes": {"iid": 3, "title": "Speciate the relay", "state": "opened",
                          "url": "https://gitlab.example/grp/proj/-/merge_requests/3"},
}


def _settings(**kw: Any) -> RelaySettings:
    base: dict[str, Any] = {"xmpp_jid": "webhook@xmpp.test", "xmpp_password": "x"}
    return RelaySettings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type]


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-GitHub-Event": "push"}, "github"),
        ({"X-Hub-Signature-256": "sha256=00"}, "github"),
        ({"X-GitHub-Delivery": "d-1"}, "github"),
        ({"X-Gitlab-Event": "Push Hook"}, "gitlab"),
        ({"X-Gitlab-Token": "t"}, "gitlab"),
        ({}, "generic"),
        ({"X-Custom": "1"}, "generic"),
    ],
)
def test_detect(headers: dict[str, str], expected: str) -> None:
    assert detect(headers).name == expected


def test_every_registered_provider_is_usable() -> None:
    for provider in PROVIDERS:
        assert provider.name
        assert provider.matches({}) is False  # generic is the only catch-all


# --- authentication ----------------------------------------------------------


def test_github_signature() -> None:
    body = b'{"a":1}'
    assert verify_signature("s3cret", body, _sign("s3cret", body))
    assert not verify_signature("s3cret", body + b" ", _sign("s3cret", body))
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "sha1=abc")
    assert not verify_signature("wrong", body, _sign("s3cret", body))


def test_gitlab_token() -> None:
    gitlab = GitLabProvider()
    assert gitlab.verify({"X-Gitlab-Token": "t0ken"}, b"", "t0ken")
    assert not gitlab.verify({"X-Gitlab-Token": "nope"}, b"", "t0ken")
    assert not gitlab.verify({}, b"", "t0ken")


def test_generic_bearer_token() -> None:
    generic = GenericProvider()
    assert generic.verify({"X-Webhook-Token": "t0ken"}, b"", "t0ken")
    assert generic.verify({"Authorization": "Bearer t0ken"}, b"", "t0ken")
    assert generic.verify({"Authorization": "bearer t0ken"}, b"", "t0ken")
    assert not generic.verify({"Authorization": "Basic t0ken"}, b"", "t0ken")
    assert not generic.verify({}, b"", "t0ken")


def test_secret_comparison_handles_non_ascii() -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str — must not 500."""
    assert not same_secret("café", "t0ken")
    assert same_secret("t0ken", "t0ken")
    assert not verify_signature("s3cret", b"{}", "sha256=café")


# --- the rule that ties them together ----------------------------------------


def test_open_only_when_nothing_is_configured() -> None:
    settings = _settings()
    assert authorised(settings, detect({}), {}, b"")


def test_a_configured_credential_closes_every_route() -> None:
    """The point of the question "how do we know it is GitHub?".

    With a GitHub secret set, an unsigned request must fail — including one
    that drops the GitHub headers to be treated as a generic sender.
    """
    settings = _settings(github_secret="s3cret")
    body = json.dumps(PR_EVENT).encode()

    signed = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign("s3cret", body)}
    assert authorised(settings, detect(signed), signed, body)

    forged = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign("guess", body)}
    assert not authorised(settings, detect(forged), forged, body)

    unsigned = {"X-GitHub-Event": "pull_request"}
    assert not authorised(settings, detect(unsigned), unsigned, body)

    # Same body, no GitHub headers at all: the generic provider has no
    # credential configured, so there is nothing to fall back to.
    assert not authorised(settings, detect({}), {}, body)


def test_shared_token_works_for_any_provider() -> None:
    settings = _settings(token="t0ken")
    headers = {"X-GitHub-Event": "push", "X-Webhook-Token": "t0ken"}
    assert authorised(settings, detect(headers), headers, b"{}")
    assert authorised(settings, detect({}), {"Authorization": "Bearer t0ken"}, b"{}")
    assert not authorised(settings, detect({}), {"X-Webhook-Token": "wrong"}, b"{}")


def test_providers_do_not_share_credentials() -> None:
    """A GitLab token must not authenticate a GitHub-shaped request."""
    settings = _settings(gitlab_token="glt")
    headers = {"X-GitHub-Event": "push", "X-Gitlab-Token": "glt"}
    assert detect(headers).name == "github"
    assert not authorised(settings, detect(headers), headers, b"{}")


# --- summaries ---------------------------------------------------------------


def test_github_summaries() -> None:
    gh = GitHubProvider()
    assert gh.summarise(PR_EVENT, {"X-GitHub-Event": "pull_request"}, "/") == (
        'GitHub pull_request.opened in o/r: #7 "Add channels" '
        "https://github.com/o/r/pull/7 (by octocat)"
    )
    push = {"ref": "refs/heads/main", "commits": [{}, {}], "compare": "https://c",
            "repository": {"full_name": "o/r"}}
    assert gh.summarise(push, {"X-GitHub-Event": "push"}, "/") == (
        "GitHub push in o/r: refs/heads/main: 2 commit(s) https://c"
    )
    run = {"action": "completed", "workflow_run": {
        "name": "CI", "status": "completed", "conclusion": "failure", "html_url": "https://r"}}
    assert gh.summarise(run, {"X-GitHub-Event": "workflow_run"}, "/") == (
        "GitHub workflow_run.completed: CI completed/failure https://r"
    )


def test_gitlab_summaries() -> None:
    gl = GitLabProvider()
    line = gl.summarise(MR_EVENT, {"X-Gitlab-Event": "Merge Request Hook"}, "/")
    assert line.startswith("GitLab Merge Request Hook in grp/proj: #3 \"Speciate the relay\"")
    assert line.endswith("(by Alice)")
    push = {"object_kind": "push", "ref": "refs/heads/main", "commits": [{}],
            "project": {"path_with_namespace": "grp/proj", "web_url": "https://gl/p"}}
    assert gl.summarise(push, {"X-Gitlab-Event": "Push Hook"}, "/") == (
        "GitLab Push Hook in grp/proj: refs/heads/main: 1 commit(s) https://gl/p"
    )


def test_generic_summary_describes_the_request() -> None:
    assert GenericProvider().summarise({"a": 1}, {}, "/hooks/ci") == "Webhook POST /hooks/ci"


@pytest.mark.parametrize("provider", [GitHubProvider(), GitLabProvider(), GenericProvider()])
@pytest.mark.parametrize(
    "payload",
    [
        None, "a string", ["a", "list"], 42,
        {"pull_request": "x"}, {"pull_request": ["x"]}, {"issue": 3},
        {"commits": 5}, {"workflow_run": "str"}, {"check_run": [1, 2]},
        {"object_attributes": "x"}, {"project": 7}, {"repository": "not-an-object"},
    ],
)
def test_summaries_survive_hostile_payloads(provider: Any, payload: Any) -> None:
    """Any sender can put any type in any field; a 500 must not be the answer."""
    for headers in ({}, {"X-GitHub-Event": "push"}, {"X-Gitlab-Event": "Push Hook"}):
        assert isinstance(provider.summarise(payload, headers, "/"), str)


# --- delivery IDs ------------------------------------------------------------


def test_delivery_ids() -> None:
    assert GitHubProvider().delivery_id({"X-GitHub-Delivery": "gh-1"}) == "gh-1"
    assert GitLabProvider().delivery_id({"X-Gitlab-Event-UUID": "gl-1"}) == "gl-1"
    assert GenericProvider().delivery_id({"X-Request-Id": "r-1"}) == "r-1"
    assert GenericProvider().delivery_id({}) is None
