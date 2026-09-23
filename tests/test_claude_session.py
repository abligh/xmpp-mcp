"""Unit tests for Claude Code session discovery and the friendly-name machinery.

No network. Session files are written to a temporary CLAUDE_CONFIG_DIR; the
process tree is injected, so the tests never see the developer's own session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from slixmpp.exceptions import PresenceError

from xmpp_mcp.claude_session import SessionWatcher, discover, read_session, resolve
from xmpp_mcp.config import Settings
from xmpp_mcp.xmpp_client import XMPPClient, XMPPError

SID = "7b3e9a41-2c5d-4f8e-9a61-0d2b8c4e7f13"
ROOM = "agents@conference.xmpp.test"


def _write(dirpath: Path, pid: int, **fields: Any) -> Path:
    sessions = dirpath / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": SID, "name": "bridge-cse-abc-18",
            "nameSource": "derived", "status": "idle", **fields}
    path = sessions / f"{pid}.json"
    path.write_text(json.dumps(data))
    return path


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(tmp_path), **extra}


# --- reading -----------------------------------------------------------------


def test_read_session(tmp_path: Path) -> None:
    s = read_session(_write(tmp_path, 7, name="Reviewer", nameSource="auto"))
    assert s is not None
    assert (s.pid, s.session_id, s.name, s.name_source, s.status) == (
        7, SID, "Reviewer", "auto", "idle")


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"pid": 1}', '{"sessionId": ""}'])
def test_unusable_files_are_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / "x.json"
    path.write_text(content)
    assert read_session(path) is None
    assert read_session(tmp_path / "missing.json") is None


def test_unknown_name_source_is_kept(tmp_path: Path) -> None:
    # The set of sources has grown before; a new one is still information.
    s = read_session(_write(tmp_path, 7, nameSource="something-new"))
    assert s is not None and s.name_source == "something-new"


# --- discovery ---------------------------------------------------------------


def test_found_via_claude_pid(tmp_path: Path) -> None:
    _write(tmp_path, 4242)
    s = discover(_env(tmp_path, CLAUDE_PID="4242"), pid=1, parent_of=lambda p: None)
    assert s is not None and s.how == "CLAUDE_PID"


def test_found_by_walking_up_the_process_tree(tmp_path: Path) -> None:
    """No CLAUDE_PID (a wrapper dropped it): the launching claude is an ancestor."""
    _write(tmp_path, 100)
    tree = {300: 200, 200: 100, 100: 1}  # us -> venv re-exec -> claude -> init
    s = discover(_env(tmp_path), pid=300, parent_of=tree.get)
    assert s is not None and s.how == "parent process" and s.pid == 100


def test_found_by_session_id_scan(tmp_path: Path) -> None:
    """Different PID namespace: the file's PID means nothing to us."""
    _write(tmp_path, 99999)
    s = discover(_env(tmp_path, CLAUDE_CODE_SESSION_ID=SID), pid=5, parent_of=lambda p: None)
    assert s is not None and s.how == "session id"


def test_a_file_for_another_session_is_rejected(tmp_path: Path) -> None:
    """A recycled PID must not hand us a stranger's identity."""
    _write(tmp_path, 4242)
    env = _env(tmp_path, CLAUDE_PID="4242", CLAUDE_CODE_SESSION_ID="someone-else")
    assert discover(env, pid=1, parent_of=lambda p: None) is None


def test_no_sessions_directory(tmp_path: Path) -> None:
    assert discover(_env(tmp_path, CLAUDE_PID="1")) is None


def test_process_tree_cycles_terminate(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    assert discover(_env(tmp_path), pid=10, parent_of={10: 11, 11: 10}.get) is None


def test_resolve_setting(tmp_path: Path) -> None:
    path = _write(tmp_path, 7)
    assert resolve("off") is None
    s = resolve(str(path))
    assert s is not None and s.how == "XMPP_CLAUDE_SESSION"


# --- watching ----------------------------------------------------------------


def _bump(path: Path, **fields: Any) -> None:
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


async def test_watcher_reports_a_rename(tmp_path: Path) -> None:
    path = _write(tmp_path, 7)
    seen: list[tuple[str | None, str | None]] = []
    w = SessionWatcher(read_session(path), lambda old, new: seen.append((old.name, new.name)))
    assert w.poll() is False  # unchanged
    _bump(path, name="Reviewer", nameSource="auto")
    assert w.poll() is True
    assert seen == [("bridge-cse-abc-18", "Reviewer")]
    assert w.session.name_source == "auto"


async def test_watcher_ignores_noise_and_strangers(tmp_path: Path) -> None:
    path = _write(tmp_path, 7)
    seen: list[Any] = []
    w = SessionWatcher(read_session(path), lambda old, new: seen.append(new))
    _bump(path, updatedAt=123)  # touched, nothing we care about changed
    assert w.poll() is False
    _bump(path, sessionId="another-session", name="Hijack")  # PID reused
    assert w.poll() is False
    assert seen == []


# --- settings ----------------------------------------------------------------


def _settings(tmp_path: Path | None = None, **kw: Any) -> Settings:
    base: dict[str, Any] = {"xmpp_jid": "bot@xmpp.test", "xmpp_password": "x"}
    if tmp_path is not None:
        base["xmpp_claude_session"] = str(tmp_path / "sessions" / "7.json")
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type]


def test_canonical_jid_from_the_session_id(tmp_path: Path) -> None:
    _write(tmp_path, 7)
    s = _settings(tmp_path, xmpp_jid="{session}@{host}", xmpp_agent_host="host1")
    assert s.xmpp_jid == f"{SID}@host1"
    assert s.xmpp_agent_id == SID


def test_session_template_without_a_session_fails() -> None:
    with pytest.raises(ValidationError, match="session"):
        _settings(xmpp_jid="{session}@xmpp.test")


def test_session_template_can_use_an_explicit_agent_id() -> None:
    """Non-Claude agents (other providers) supply their own stable ID."""
    s = _settings(xmpp_jid="{session}@xmpp.test", xmpp_agent_id="gemini-7")
    assert s.xmpp_jid == "gemini-7@xmpp.test"


def test_friendly_name_and_nick_come_from_the_session(tmp_path: Path) -> None:
    _write(tmp_path, 7, name="Reviewer", nameSource="auto")
    s = _settings(tmp_path, xmpp_agent_name="ignored-for-display")
    assert s.display_name == "Reviewer"
    assert s.xmpp_nick == "Reviewer"
    assert not s.display_name_is_fixed


def test_explicit_display_name_pins_the_name(tmp_path: Path) -> None:
    _write(tmp_path, 7, name="Reviewer")
    s = _settings(tmp_path, xmpp_display_name="Code Reviewer")
    assert s.display_name == "Code Reviewer" and s.display_name_is_fixed


# --- the client: renames, addressing, names on inbound traffic --------------


def _client(tmp_path: Path | None = None, **kw: Any) -> XMPPClient:
    return XMPPClient(_settings(tmp_path, **kw))


async def test_client_starts_with_the_session_name(tmp_path: Path) -> None:
    _write(tmp_path, 7, name="Reviewer", nameSource="auto")
    c = _client(tmp_path)
    assert (c.friendly_name, c.name_source) == ("Reviewer", "auto")
    pres = c._stamp_agent_presence(c.xmpp.make_presence())
    agent = pres.xml.find("{urn:xmpp-mcp:agent:0}agent")
    assert (agent.get("name"), agent.get("name-source")) == ("Reviewer", "auto")


async def test_rename_reaches_roster_and_every_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, 7)
    c = _client(tmp_path)
    c._joined_rooms = {ROOM: "bridge-cse-abc-18", "fixed@conference.xmpp.test": "pinned"}
    c._nick_follows = {ROOM}
    sent: list[str | None] = []
    monkeypatch.setattr(c.xmpp, "is_connected", lambda: True)
    monkeypatch.setattr(c.xmpp, "send_presence", lambda pto=None, **kw: sent.append(pto))

    async def no_pep() -> None:
        return None

    monkeypatch.setattr(c, "_publish_nick", no_pep)
    await c.rename("Reviewer", "auto")
    assert c.friendly_name == "Reviewer"
    assert sent == [
        None,                                   # broadcast to the roster
        f"{ROOM}/Reviewer",                     # nick change where it follows
        "fixed@conference.xmpp.test/pinned",    # status refresh where pinned
    ]


async def test_session_rename_is_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, 7)
    c = _client(tmp_path)
    renamed: list[tuple[str, str | None]] = []

    async def rename(name: str, source: str | None = None) -> None:
        renamed.append((name, source))

    monkeypatch.setattr(c, "rename", rename)
    w = SessionWatcher(read_session(path), c._on_claude_session_change)
    _bump(path, name="Reviewer", nameSource="auto")
    w.poll()
    import asyncio
    await asyncio.sleep(0)
    assert renamed == [("Reviewer", "auto")]


def _peer(c: XMPPClient, full: str, name: str, agent_id: str, real: str | None = None) -> None:
    pres = c.xmpp.make_presence(pfrom=full, pto="bot@xmpp.test/r")
    info = pres["mcp_agent"]
    info["id"], info["name"] = agent_id, name
    if real:
        from xml.etree import ElementTree as ET
        x = ET.SubElement(pres.xml, "{http://jabber.org/protocol/muc#user}x")
        ET.SubElement(x, "{http://jabber.org/protocol/muc#user}item", {"jid": real})
    c.presence.update(pres)


def _in_room(c: XMPPClient, monkeypatch: pytest.MonkeyPatch, occupants: dict[str, str]) -> None:
    c._joined_rooms[ROOM] = "bot"
    muc = c.xmpp.plugin["xep_0045"]
    monkeypatch.setattr(muc, "get_roster", lambda room: ["bot", *occupants])
    monkeypatch.setattr(muc, "get_jid_property",
                        lambda room, nick, prop: occupants.get(nick) if prop == "jid" else None)


async def test_resolve_address(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    _in_room(c, monkeypatch, {"Reviewer": "sess-r@xmpp.test/x", "Builder": "sess-b@xmpp.test/y"})
    _peer(c, f"{ROOM}/Reviewer", "Reviewer", "sess-r", real="sess-r@xmpp.test/x")
    _peer(c, f"{ROOM}/Builder", "Builder", "sess-b", real="sess-b@xmpp.test/y")

    assert c.resolve_address("alice@xmpp.test") == "alice@xmpp.test"  # JIDs pass through
    assert c.resolve_address("reviewer") == "sess-r@xmpp.test"          # friendly, any case
    assert c.resolve_address("sess-b") == "sess-b@xmpp.test"            # internal agent ID
    with pytest.raises(XMPPError, match="No agent"):
        c.resolve_address("Nobody")


async def test_ambiguous_names_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Friendly names are not unique: never guess which one was meant."""
    c = _client()
    _in_room(c, monkeypatch, {"Reviewer": "a@xmpp.test/x", "Reviewer (host2)": "b@xmpp.test/y"})
    _peer(c, f"{ROOM}/Reviewer", "Reviewer", "a", real="a@xmpp.test/x")
    _peer(c, f"{ROOM}/Reviewer (host2)", "Reviewer", "b", real="b@xmpp.test/y")
    with pytest.raises(XMPPError, match="ambiguous") as exc:
        c.resolve_address("Reviewer")
    assert "a@xmpp.test" in str(exc.value) and "b@xmpp.test" in str(exc.value)
    assert c.resolve_address("Reviewer (host2)") == "b@xmpp.test"  # the nick is unique


async def test_inbound_messages_carry_the_senders_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _client()
    _in_room(c, monkeypatch, {"Reviewer": "sess-r@xmpp.test/x"})
    _peer(c, f"{ROOM}/Reviewer", "Reviewer", "sess-r", real="sess-r@xmpp.test/x")
    got: list[dict[str, Any]] = []
    c.add_message_listener(got.append)
    # A direct message from the same agent: known to us only through the room.
    c._on_message(c.xmpp.make_message(mto="bot@xmpp.test", mbody="hi", mtype="chat",
                                      mfrom="sess-r@xmpp.test/x"))
    assert got[0]["sender_name"] == "Reviewer"


async def test_a_taken_nick_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sessions may share a friendly name; the second still gets into the room."""
    c = _client(xmpp_agent_name="Reviewer", xmpp_agent_host="host2")
    muc = c.xmpp.plugin["xep_0045"]
    tried: list[str] = []

    async def join(room: str, nick: str, **kw: Any) -> None:
        tried.append(nick)
        if nick == "Reviewer":
            err = c.xmpp.make_presence(pfrom=f"{room}/{nick}", ptype="error")
            err["error"]["condition"] = "conflict"
            raise PresenceError(err)
        muc.our_nicks.setdefault(None, {})[room] = nick

    monkeypatch.setattr(muc, "join_muc_wait", join)
    monkeypatch.setattr(muc, "get_roster", lambda room: [])
    joined = await c.join_room(ROOM)
    assert tried == ["Reviewer", "Reviewer (host2)"]
    assert joined["nick"] == "Reviewer (host2)"
    assert ROOM in c._nick_follows


async def test_an_explicit_nick_is_not_second_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(xmpp_agent_name="Reviewer")
    muc = c.xmpp.plugin["xep_0045"]

    async def join(room: str, nick: str, **kw: Any) -> None:
        err = c.xmpp.make_presence(pfrom=f"{room}/{nick}", ptype="error")
        err["error"]["condition"] = "conflict"
        raise PresenceError(err)

    monkeypatch.setattr(muc, "join_muc_wait", join)
    with pytest.raises(XMPPError, match="conflict"):
        await c.join_room(ROOM, "exactly-this")


# --- presence follows busy/idle ------------------------------------------------


def test_session_status_maps_to_presence(tmp_path: Path) -> None:
    from xmpp_mcp.xmpp_client import _session_presence

    assert _session_presence(read_session(_write(tmp_path, 1, status="busy"))) == ("dnd", "busy")
    assert _session_presence(read_session(_write(tmp_path, 2, status="idle"))) == (None, "idle")
    assert _session_presence(None) == (None, None)


async def test_busy_idle_is_announced_until_presence_is_set_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, 7, status="idle")
    c = _client(tmp_path)
    c._joined_rooms = {ROOM: "bridge-cse-abc-18"}
    sent: list[tuple[str | None, str | None, str | None]] = []
    monkeypatch.setattr(c.xmpp, "is_connected", lambda: True)
    monkeypatch.setattr(c.xmpp, "send_presence",
                        lambda pto=None, pshow=None, pstatus=None: sent.append((pto, pshow, pstatus)))
    w = SessionWatcher(read_session(path), c._on_claude_session_change)

    _bump(path, status="busy")
    w.poll()
    assert sent == [(None, "dnd", "busy"), (f"{ROOM}/bridge-cse-abc-18", "dnd", "busy")]

    c.set_presence("away", "at lunch")  # an explicit choice wins from now on
    sent.clear()
    _bump(path, status="idle")
    w.poll()
    assert sent == []
    assert c._presence == ("away", "at lunch")


# --- the friendly name travels in the first message ----------------------------


async def test_nick_goes_in_the_first_message_to_each_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XEP-0172 §4.2: the nick accompanies the first message to a contact."""
    _write(tmp_path, 7, name="Reviewer")
    c = _client(tmp_path)
    sent: list[Any] = []
    monkeypatch.setattr(c.xmpp, "send", sent.append)

    def nick_of(stanza: Any) -> str | None:
        el = stanza.xml.find("{http://jabber.org/protocol/nick}nick")
        return el.text if el is not None else None

    c.send_chat("alice@xmpp.test", "one")
    c.send_chat("alice@xmpp.test/phone", "two")  # same contact, any resource
    c.send_chat("bob@xmpp.test", "three")
    assert [nick_of(m) for m in sent] == ["Reviewer", None, "Reviewer"]

    async def no_pep() -> None:
        return None

    monkeypatch.setattr(c, "_publish_nick", no_pep)
    monkeypatch.setattr(c.xmpp, "is_connected", lambda: False)
    await c.rename("Code Reviewer", "user")
    sent.clear()
    c.send_chat("alice@xmpp.test", "after the rename")
    assert nick_of(sent[0]) == "Code Reviewer"  # contacts learn the new name


async def test_no_nick_in_a_private_message_to_a_room_occupant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, 7, name="Reviewer")
    c = _client(tmp_path)
    c._known_rooms.add(ROOM)
    sent: list[Any] = []
    monkeypatch.setattr(c.xmpp, "send", sent.append)
    c.send_chat(f"{ROOM}/alice", "psst")  # our nick in the room already says who we are
    assert sent[0].xml.find("{http://jabber.org/protocol/nick}nick") is None


async def test_a_nick_in_a_message_names_a_sender_we_cannot_otherwise_see() -> None:
    """No shared room, no roster: the message's own <nick/> is the only name."""
    c = _client()
    got: list[dict[str, Any]] = []
    c.add_message_listener(got.append)
    first = c.xmpp.make_message(mto="bot@xmpp.test", mbody="hello", mtype="chat",
                                mfrom="sess-z.host9@xmpp.test/r")
    first["nick"]["nick"] = "Zed"
    c._on_message(first)
    later = c.xmpp.make_message(mto="bot@xmpp.test", mbody="again", mtype="chat",
                                mfrom="sess-z.host9@xmpp.test/r")
    c._on_message(later)  # no nick this time: remembered from the first
    assert [g["sender_name"] for g in got] == ["Zed", "Zed"]
    # ...and that name is enough to write back.
    assert c.resolve_address("zed") == "sess-z.host9@xmpp.test"
