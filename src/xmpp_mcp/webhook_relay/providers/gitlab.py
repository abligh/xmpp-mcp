"""GitLab: a shared token echoed in ``X-Gitlab-Token``, and its event shapes.

Included as a second real provider so the seam is exercised rather than
theoretical: GitLab authenticates completely differently from GitHub (a
plain shared secret in a header, not a signature over the body), which is
exactly the variation the provider interface exists to absorb.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..auth import same_secret
from .base import Provider, count, get, obj


class GitLabProvider(Provider):
    name = "gitlab"

    def matches(self, headers: Mapping[str, Any]) -> bool:
        return bool(headers.get("X-Gitlab-Event") or headers.get("X-Gitlab-Token"))

    def verify(self, headers: Mapping[str, Any], body: bytes, credential: str) -> bool:
        given = headers.get("X-Gitlab-Token") or ""
        # Weaker than GitHub's scheme by design: the token is replayable by
        # anyone who sees one request, so it needs a TLS-terminating front end.
        return bool(given) and same_secret(given, credential)

    def delivery_id(self, headers: Mapping[str, Any]) -> str | None:
        return headers.get("X-Gitlab-Event-UUID")

    def event(self, headers: Mapping[str, Any], payload: Any) -> str | None:
        return headers.get("X-Gitlab-Event") or get(payload, "object_kind")

    def summarise(self, payload: Any, headers: Mapping[str, Any], path: str) -> str:
        event = headers.get("X-Gitlab-Event") or get(payload, "object_kind") or "event"
        project = get(payload, "project", "path_with_namespace") or get(payload, "project", "name")
        who = get(payload, "user_name") or get(payload, "user", "username")

        head = f"GitLab {event}"
        if project:
            head += f" in {project}"

        attrs = obj(payload, "object_attributes")
        detail = ""
        if attrs:
            title = attrs.get("title") or attrs.get("note") or ""
            state = attrs.get("state") or attrs.get("action") or ""
            url = attrs.get("url") or ""
            number = attrs.get("iid")
            detail = f'{f"#{number} " if number else ""}"{title}" {state} {url}'
        elif get(payload, "object_kind") == "push" or event == "Push Hook":
            detail = (
                f'{get(payload, "ref")}: {count(get(payload, "commits"))} commit(s) '
                f'{get(payload, "project", "web_url") or ""}'
            )

        line = head + (f": {detail.strip()}" if detail.strip() else "")
        return line + (f" (by {who})" if who else "")
