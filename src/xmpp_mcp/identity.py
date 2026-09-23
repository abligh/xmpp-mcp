"""Per-agent XMPP identities derived from the agent name and host.

Each Claude Code session (an "agent") runs its own xmpp-mcp server, so each
one needs its own JID. Rather than hand-writing a JID per session, ``XMPP_JID``
may be a *template*:

    XMPP_JID={session}@{host}                 # canonical: one vhost per host
    XMPP_JID={session}.{host}@xmpp.example.com # canonical: one shared domain
    XMPP_JID={agent}.{host}@xmpp.example.com   # a name you choose

Placeholders:

``{session}``  the Claude Code session ID (see :mod:`xmpp_mcp.claude_session`)
``{agent}``    the agent name (``XMPP_AGENT_NAME`` / ``--agent-name``)
``{host}``     short hostname (``XMPP_AGENT_HOST`` overrides ``gethostname()``)
``{fqdn}``     fully-qualified hostname (``socket.getfqdn()``)

``{session}`` is the one to build a *canonical* address from: it is unique
and never changes for the life of the session, unlike the name Claude Code
shows, which starts out derived from the directory and is later replaced.
That shown name becomes the agent's *friendly* name instead — advertised to
peers and accepted as an address (see ``XMPPClient.resolve_address``).

Substituted values are normalised so the result is always a valid JID
(RFC 7622): lowercased (localparts and domains compare case-insensitively
anyway, so this makes the address canonical) and restricted to
``[a-z0-9.-]``. Anything else — spaces, ``@``, ``/``, ``_``, quotes, ``:``
and the other characters RFC 7622 §3.3.1 forbids in a localpart — collapses
to ``-``. The same alphabet is valid in a DNS label, so a placeholder works
in the domain part too.

Normalisation is **lossy and not collision-free**: ``review api``,
``review-api`` and ``review@api`` all become ``review-api``, and a name with
no ASCII at all (``日本語``) has no usable form and raises. Two agents that
normalise to the same localpart share one account — give agents distinct
ASCII names, or put the host in the template.
"""

from __future__ import annotations

import re
import socket
import string

# RFC 7622 §3.3.1 bans " & ' / : < > @ and whitespace from localparts; the
# domainpart must be a DNS name. This alphabet satisfies both, so a
# placeholder is safe on either side of the "@". Underscore is deliberately
# excluded: it is legal in a localpart but not in a hostname, and a template
# may well put {host} in the domain.
_UNSAFE = re.compile(r"[^a-z0-9.-]+")
# Leading/trailing separators make ugly (and, in a DNS label, invalid) names.
_EDGES = "-._"

PLACEHOLDERS = frozenset({"session", "agent", "host", "fqdn"})


class IdentityError(ValueError):
    """Raised when a JID template cannot be expanded into a valid identity."""


def normalise(value: str) -> str:
    """Normalise ``value`` into a JID/DNS-safe token (lowercase, ``[a-z0-9.-]``).

    Raises :class:`IdentityError` if nothing usable is left.
    """
    token = _UNSAFE.sub("-", value.strip().lower()).strip(_EDGES)
    token = re.sub(r"-{2,}", "-", token)
    if not token:
        raise IdentityError(f"{value!r} normalises to an empty JID component")
    return token


def short_hostname() -> str:
    """The machine's short hostname (first label of ``gethostname()``)."""
    return socket.gethostname().split(".", 1)[0]


def template_fields(template: str) -> set[str]:
    """Return the placeholder names used in ``template``.

    Raises :class:`IdentityError` for unknown placeholders or format specs, so
    a typo like ``{agnet}`` fails loudly at startup instead of producing a
    literal ``{agnet}`` in the JID.
    """
    fields: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:  # e.g. an unbalanced "{"
        raise IdentityError(f"Malformed JID template {template!r}: {exc}") from exc
    for _literal, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            raise IdentityError(
                f"Unknown placeholder {{{name}}} in {template!r}; "
                f"expected one of {sorted(PLACEHOLDERS)}"
            )
        if spec or conversion:
            raise IdentityError(f"Format specs are not supported in {template!r}")
        fields.add(name)
    return fields


def expand_template(
    template: str,
    *,
    session: str | None = None,
    agent: str | None = None,
    host: str | None = None,
    fqdn: str | None = None,
) -> str:
    """Expand ``{session}`` / ``{agent}`` / ``{host}`` / ``{fqdn}`` in ``template``.

    A template with no placeholders is returned unchanged (so plain
    ``XMPP_JID=bot@example.com`` keeps working). ``host`` / ``fqdn`` default to
    this machine's names; ``agent`` has no default and must be supplied when
    the template uses it.
    """
    fields = template_fields(template)
    if not fields:
        return template
    values: dict[str, str] = {}
    if "session" in fields:
        if not session:
            raise IdentityError(
                f"JID template {template!r} uses {{session}} but no Claude Code "
                "session was found (set XMPP_AGENT_ID, or run under Claude Code)"
            )
        values["session"] = normalise(session)
    if "agent" in fields:
        if not agent:
            raise IdentityError(
                f"JID template {template!r} uses {{agent}} but no agent name is set "
                "(set XMPP_AGENT_NAME or pass --agent-name)"
            )
        values["agent"] = normalise(agent)
    if "host" in fields:
        values["host"] = normalise(host or short_hostname())
    if "fqdn" in fields:
        values["fqdn"] = normalise(fqdn or socket.getfqdn())
    return template.format_map(values)
