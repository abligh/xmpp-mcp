"""Unit tests for host-scoped derived credentials (xmpp_mcp.credentials)."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path

import pytest

from xmpp_mcp.credentials import (
    CredentialError, HostKey, derive_host_key, load_host_key, load_master, main,
    mint_password, split_localpart, verify_password,
)

MASTER = bytes(range(32))
NOW = 1_800_000_000
JID = "4f0c2a9e-1111-2222-3333-444455556666.host1@agents.example.com"


def test_derivation_matches_the_published_formula() -> None:
    """Pin the exact bytes, so the server module (Lua) can be checked against it."""
    host_key = hmac.new(MASTER, b"xmpp-mcp host v1|host1", hashlib.sha256).digest()
    assert derive_host_key(MASTER, "host1") == host_key
    mac = hmac.new(host_key, f"xmpp-mcp agent v1|{JID}|{NOW}".encode(), hashlib.sha256).digest()
    import base64
    token = base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    assert mint_password(host_key, JID, NOW) == f"xmc1.{NOW}.{token}"


def test_a_minted_password_verifies() -> None:
    key = HostKey("host1", derive_host_key(MASTER, "host1"))
    pw = key.password_for(JID, ttl=3600, now=NOW)
    assert verify_password(MASTER, JID, pw, now=NOW)


def test_a_host_cannot_mint_for_another_host() -> None:
    """host1's key, host2's JID: refused by the client, and useless at the server."""
    key1 = HostKey("host1", derive_host_key(MASTER, "host1"))
    other = JID.replace(".host1@", ".host2@")
    with pytest.raises(CredentialError, match="host2"):
        key1.password_for(other)
    forged = mint_password(key1.key, other, NOW + 60)  # bypassing the client check
    assert not verify_password(MASTER, other, forged, now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p[:-1] + ("A" if p[-1] != "A" else "B"),        # tampered MAC
        lambda p: p.replace("xmc1.", "xmc2."),                     # unknown version
        lambda p: "xmc1." + str(NOW + 999) + "." + p.split(".")[2],  # moved expiry
        lambda p: "hunter2",                                        # a plain password
    ],
)
def test_tampering_is_rejected(mutate) -> None:
    key = HostKey("host1", derive_host_key(MASTER, "host1"))
    pw = key.password_for(JID, ttl=3600, now=NOW)
    assert not verify_password(MASTER, JID, mutate(pw), now=NOW)


def test_bound_to_the_whole_jid() -> None:
    """Same session and host, another domain: the password does not transfer."""
    key = HostKey("host1", derive_host_key(MASTER, "host1"))
    pw = key.password_for(JID, ttl=3600, now=NOW)
    assert not verify_password(MASTER, JID.replace("example.com", "evil.test"), pw, now=NOW)


def test_expiry_and_lifetime_are_enforced() -> None:
    key = HostKey("host1", derive_host_key(MASTER, "host1"))
    pw = key.password_for(JID, ttl=60, now=NOW)
    assert verify_password(MASTER, JID, pw, now=NOW + 60 + 299)      # within the skew
    assert not verify_password(MASTER, JID, pw, now=NOW + 60 + 301)  # expired
    long_lived = key.password_for(JID, ttl=30 * 86400, now=NOW)
    assert not verify_password(MASTER, JID, long_lived, now=NOW)     # beyond max TTL


@pytest.mark.parametrize("jid", ["plain@example.com", ".host1@x", "session.@x", "no-at-sign"])
def test_only_agent_shaped_jids(jid: str) -> None:
    with pytest.raises(CredentialError):
        split_localpart(jid)


def test_split_is_at_the_first_dot() -> None:
    """Hosts may contain dots; sessions may not."""
    assert split_localpart("sess.build.box@x") == ("sess", "build.box")


# --- key files and the CLI -------------------------------------------------------


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    master, host = tmp_path / "master.key", tmp_path / "host1.key"
    assert main(["new-master", "-o", str(master)]) == 0
    assert main(["host-key", "--master", str(master), "--host", "host1", "-o", str(host)]) == 0
    for path in (master, host):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # private from creation
    key = load_host_key(host)
    assert key.host == "host1" and key.key == derive_host_key(load_master(master), "host1")
    assert main(["password", "--host-key", str(host), "--jid", JID]) == 0
    pw = capsys.readouterr().out.strip()
    assert verify_password(load_master(master), JID, pw)


def test_cli_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["host-key", "--master", str(tmp_path / "nope"), "--host", "h",
                 "-o", str(tmp_path / "out")]) == 1
    assert "cannot read master key" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ["", "host = h\n", "key = 00\n", "host = h\nkey = zz\n", "host = h\nkey = " + "00" * 16],
)
def test_malformed_host_key_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.key"
    path.write_text(content)
    with pytest.raises(CredentialError):
        load_host_key(path)


def test_a_readable_key_file_is_flagged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "h.key"
    path.write_text(f"host = h\nkey = {'00' * 32}\n")
    os.chmod(path, 0o644)
    load_host_key(path)
    assert "should be 0600" in caplog.text


def test_settings_accept_a_host_key_instead_of_a_password(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from xmpp_mcp.config import Settings

    s = Settings(_env_file=None, xmpp_jid="a.h@x.test",  # type: ignore[call-arg]
                 xmpp_host_key_file=str(tmp_path / "h.key"))
    assert s.xmpp_password is None
    with pytest.raises(ValidationError, match="XMPP_HOST_KEY_FILE"):
        Settings(_env_file=None, xmpp_jid="a.h@x.test")  # type: ignore[call-arg]


async def test_the_client_refuses_a_key_for_another_host(tmp_path: Path) -> None:
    """Fail at startup with a clear message, not with an opaque login failure."""
    from xmpp_mcp.config import Settings
    from xmpp_mcp.xmpp_client import XMPPClient, XMPPError

    path = tmp_path / "h.key"
    path.write_text(f"host = host1\nkey = {derive_host_key(MASTER, 'host1').hex()}\n")
    os.chmod(path, 0o600)
    s = Settings(_env_file=None, xmpp_jid="sess.host2@x.test",  # type: ignore[call-arg]
                 xmpp_host_key_file=str(path))
    with pytest.raises(XMPPError, match="host1"):
        XMPPClient(s)
