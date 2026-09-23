"""Making arbitrary payload text safe to put inside an XMPP stanza.

Generic: nothing here knows or cares which system sent the webhook.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape as xml_escape

_TRUNCATED = "\n…[truncated]"

# Characters XML 1.0 §2.2 forbids in character data. A webhook payload is
# arbitrary bytes from outside: a single 0x0C (or a lone surrogate smuggled in
# as a \uXXXX escape) inside a message body makes the server's parser abort the
# whole stream, which drops every queued message and looks like a successful
# send from here. Everything outbound is scrubbed exactly once, in
# format_body / scrub.
_ILLEGAL_XML = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def scrub(text: str) -> str:
    """Replace characters that are not legal XML 1.0 character data."""
    return _ILLEGAL_XML.sub("\ufffd", text)


def xml_cost(text: str) -> int:
    """Bytes ``text`` occupies in a stanza, after XML escaping and UTF-8 encoding.

    Stanza size limits are counted in bytes on the wire, not code points: one
    emoji is 4 bytes and one "&" becomes 5 ("&amp;"), so a character-counted
    budget can be exceeded several times over.
    """
    return len(xml_escape(text).encode("utf-8"))


def truncate_to_bytes(text: str, limit: int) -> str:
    """Longest prefix of ``text`` whose :func:`xml_cost` is within ``limit``."""
    if xml_cost(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if xml_cost(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def format_body(summary: str, raw: str, limit: int) -> str:
    """Summary line + payload, scrubbed and truncated to ``limit`` stanza bytes.

    ``limit`` counts bytes after XML escaping (see :func:`_xml_cost`), so the
    result is safe to put in a stanza whatever the payload contained.
    """
    body = scrub(f"{summary}\n\n{raw}" if raw else summary)
    if xml_cost(body) <= limit:
        return body
    budget = limit - xml_cost(_TRUNCATED)
    if budget <= 0:
        # Pathologically small limit: the marker alone is the whole message.
        return truncate_to_bytes(_TRUNCATED, limit)
    return truncate_to_bytes(body, budget) + _TRUNCATED
