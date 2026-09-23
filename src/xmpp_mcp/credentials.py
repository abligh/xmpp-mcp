"""Host-scoped derived credentials: one secret per host, one identity per session.

Every agent on a host can read every other agent's files, so a password per
agent adds nothing *within* a host — the host is the trust boundary. This
scheme gives each host one secret and still gives each session its own
identity and its own password, derived on demand:

    master   = 32 random bytes, held only by the XMPP server
    host_key = HMAC-SHA256(master,   "xmpp-mcp host v1|"  + host)
    mac      = HMAC-SHA256(host_key, "xmpp-mcp agent v1|" + bare_jid + "|" + expiry)
    password = "xmc1." + expiry + "." + base64url(mac)

for a JID of the form ``<session>.<host>@<agents domain>``. The server holds
only ``master``; it takes ``host`` from the JID being authenticated, derives
that host's key, recomputes the MAC and compares. So:

* a host holds just its own ``host_key`` and can mint passwords only for
  ``*.<its host>@…`` — host1 cannot impersonate host2's agents;
* the MAC covers the whole bare JID, so a password is useless on any other
  domain or account;
* ``expiry`` (Unix seconds) bounds the damage of a password leaked from a
  log or a process listing — agents mint a fresh one on every connect;
* the ``… v1|`` labels keep these keys from ever being valid for anything
  else.

HMAC rather than a hash of a concatenation: ``H(a|bc)`` and ``H(ab|c)`` would
collide, and plain SHA-256 permits length extension.

The session part of the localpart must not contain ``.``: the server splits
``<session>.<host>`` at the first dot. Claude Code session IDs (UUIDs) never
do. The login itself is SASL PLAIN, so it must run over TLS.

The server side is ``contrib/prosody/mod_auth_xmpp_mcp.lua``,
which implements the same derivation inside Prosody.

Key files are small text files::

    # xmpp-mcp host key v1
    host = host1
    key = 5f0c…

and should be mode 0600, readable only by the agents' user.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import logging
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("xmpp_mcp.credentials")

HOST_LABEL = b"xmpp-mcp host v1|"
AGENT_LABEL = b"xmpp-mcp agent v1|"
PASSWORD_VERSION = "xmc1"
DEFAULT_TTL = 24 * 3600
_HEADER = "# xmpp-mcp host key v1"


class CredentialError(ValueError):
    """A key file is unusable, or a JID cannot carry a derived credential."""


def derive_host_key(master: bytes, host: str) -> bytes:
    return hmac.new(master, HOST_LABEL + host.encode("utf-8"), hashlib.sha256).digest()


def split_localpart(bare_jid: str) -> tuple[str, str]:
    """``<session>.<host>@domain`` → ``(session, host)``, split at the first dot."""
    local, at, _domain = bare_jid.partition("@")
    session, dot, host = local.partition(".")
    if not at or not dot or not session or not host:
        raise CredentialError(
            f"{bare_jid!r} is not an agent JID of the form <session>.<host>@<domain>"
        )
    return session, host


def mint_password(host_key: bytes, bare_jid: str, expiry: int) -> str:
    """The derived password for ``bare_jid``, valid until ``expiry``."""
    msg = AGENT_LABEL + f"{bare_jid}|{expiry}".encode("utf-8")
    mac = hmac.new(host_key, msg, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return f"{PASSWORD_VERSION}.{expiry}.{token}"


def verify_password(master: bytes, bare_jid: str, password: str,
                    now: float | None = None, max_ttl: int = 7 * 24 * 3600,
                    skew: int = 300) -> bool:
    """What the server does — used by tests to pin the two implementations together."""
    try:
        version, expiry_text, token = password.split(".", 2)
        expiry = int(expiry_text)
        _session, host = split_localpart(bare_jid)
    except (ValueError, CredentialError):
        return False
    now = time.time() if now is None else now
    if version != PASSWORD_VERSION or expiry < now - skew or expiry > now + max_ttl:
        return False
    expected = mint_password(derive_host_key(master, host), bare_jid, expiry)
    return hmac.compare_digest(expected.encode(), password.encode())


@dataclass(frozen=True)
class HostKey:
    host: str
    key: bytes

    def password_for(self, bare_jid: str, ttl: int = DEFAULT_TTL,
                     now: float | None = None) -> str:
        """Mint a password for one of this host's JIDs, refusing any other host's."""
        _session, host = split_localpart(bare_jid)
        if host != self.host:
            raise CredentialError(
                f"this host key is for host {self.host!r}, but {bare_jid!r} belongs to "
                f"host {host!r} — check XMPP_AGENT_HOST / the JID template"
            )
        expiry = int(time.time() if now is None else now) + ttl
        return mint_password(self.key, bare_jid, expiry)


def _warn_if_exposed(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning("%s is readable by group/others (mode %o); it should be 0600",
                       path, stat.S_IMODE(mode))


def load_host_key(path: str | Path) -> HostKey:
    """Read a host key file written by ``xmpp-mcp-keys host-key``."""
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"cannot read host key file {p}: {exc}") from exc
    _warn_if_exposed(p)
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, eq, value = line.partition("=")
        if eq:
            fields[name.strip()] = value.strip()
    try:
        key = bytes.fromhex(fields["key"])
        host = fields["host"]
    except (KeyError, ValueError) as exc:
        raise CredentialError(f"{p} is not a host key file (need 'host =' and 'key =')") from exc
    if len(key) != 32 or not host:
        raise CredentialError(f"{p}: malformed host key")
    return HostKey(host=host, key=key)


def load_master(path: str | Path) -> bytes:
    p = Path(path).expanduser()
    try:
        raw = p.read_text(encoding="utf-8").strip()
        key = bytes.fromhex(raw)
    except (OSError, ValueError) as exc:
        raise CredentialError(f"cannot read master key {p}: {exc}") from exc
    if len(key) != 32:
        raise CredentialError(f"{p}: a master key is 32 bytes of hex")
    _warn_if_exposed(p)
    return key


def _write_private(path: Path, text: str) -> None:
    """Create ``path`` 0600 from the start (never briefly world-readable)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def main(argv: list[str] | None = None) -> int:
    """``xmpp-mcp-keys``: generate the master key, derive host keys, mint passwords."""
    parser = argparse.ArgumentParser(
        prog="xmpp-mcp-keys",
        description="Manage host-scoped derived XMPP credentials (see xmpp_mcp.credentials).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_master = sub.add_parser("new-master", help="generate the server's master key")
    p_master.add_argument("-o", "--output", required=True, help="file to create (mode 0600)")
    p_host = sub.add_parser("host-key", help="derive one host's key from the master key")
    p_host.add_argument("--master", required=True, help="master key file")
    p_host.add_argument("--host", required=True, help="host name, as it appears in JIDs")
    p_host.add_argument("-o", "--output", required=True, help="file to create (mode 0600)")
    p_pw = sub.add_parser("password", help="mint a password (for testing/debugging)")
    p_pw.add_argument("--host-key", required=True)
    p_pw.add_argument("--jid", required=True)
    p_pw.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "new-master":
            _write_private(Path(args.output), secrets.token_bytes(32).hex() + "\n")
        elif args.cmd == "host-key":
            key = derive_host_key(load_master(args.master), args.host)
            _write_private(Path(args.output),
                           f"{_HEADER}\nhost = {args.host}\nkey = {key.hex()}\n")
        else:
            print(load_host_key(args.host_key).password_for(args.jid, ttl=args.ttl))
    except CredentialError as exc:
        print(f"xmpp-mcp-keys: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
