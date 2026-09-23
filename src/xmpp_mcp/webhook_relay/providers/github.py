"""GitHub: ``X-Hub-Signature-256`` HMAC, and summaries for its event types."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from ..auth import same_secret
from .base import Provider, count, get, obj


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check ``X-Hub-Signature-256: sha256=<hex hmac of the raw body>``.

    Note what this does and does not prove: it proves the body was produced by
    someone holding the secret. It says nothing about *where* the request was
    aimed — the path, query and headers that pick the XMPP target are outside
    the signature — and it carries no timestamp, so a captured delivery stays
    valid for ever. The relay compensates by remembering delivery IDs and by
    restricting the addressable JIDs.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return same_secret(header.removeprefix("sha256="), expected)


class GitHubProvider(Provider):
    name = "github"

    def matches(self, headers: Mapping[str, Any]) -> bool:
        # GitHub always sends all three; any one of them marks the claim.
        return any(headers.get(h) for h in
                   ("X-GitHub-Event", "X-GitHub-Delivery", "X-Hub-Signature-256"))

    def verify(self, headers: Mapping[str, Any], body: bytes, credential: str) -> bool:
        return verify_signature(credential, body, headers.get("X-Hub-Signature-256"))

    def delivery_id(self, headers: Mapping[str, Any]) -> str | None:
        return headers.get("X-GitHub-Delivery")

    def event(self, headers: Mapping[str, Any], payload: Any) -> str | None:
        return headers.get("X-GitHub-Event")

    def summarise(self, payload: Any, headers: Mapping[str, Any], path: str) -> str:
        event = headers.get("X-GitHub-Event") or "event"
        action = get(payload, "action")
        repo = get(payload, "repository", "full_name")
        who = get(payload, "sender", "login")

        head = f"GitHub {event}{'.' + action if action else ''}"
        if repo:
            head += f" in {repo}"

        detail = ""
        if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
            pr = obj(payload, "pull_request")
            detail = f'#{pr.get("number")} "{pr.get("title")}" {pr.get("html_url", "")}'
        elif event in ("issues", "issue_comment"):
            issue = obj(payload, "issue")
            detail = f'#{issue.get("number")} "{issue.get("title")}" {issue.get("html_url", "")}'
        elif event == "push":
            detail = (
                f'{get(payload, "ref")}: {count(get(payload, "commits"))} commit(s) '
                f'{get(payload, "compare") or ""}'
            )
        elif event in ("workflow_run", "check_run", "check_suite"):
            run = obj(payload, event)
            detail = (
                f'{run.get("name") or ""} {run.get("status") or ""}'
                f'/{run.get("conclusion") or "-"} {run.get("html_url") or ""}'
            )

        line = head + (f": {detail.strip()}" if detail.strip() else "")
        return line + (f" (by {who})" if who else "")
