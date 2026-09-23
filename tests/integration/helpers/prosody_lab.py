"""Bring up the Prosody lab: throwaway CA, server certificate, keys, accounts.

Used by ``start-lab-prosody.py`` and by the pytest ``prosody`` fixture. All
generated material lives in ``tests/integration/docker/prosody/generated/``
(gitignored) and is created once, then reused.

What gets created:

* ``certs/ca.crt`` + ``certs/lab.crt`` / ``lab.key`` — a private CA and a
  server certificate for xmpp.test, agents.xmpp.test and conference.xmpp.test.
  Clients verify against ``ca.crt`` (``XMPP_CA_FILE``) — real verification, no
  ``XMPP_TLS_INSECURE``.
* ``secrets/master.key`` — the server's master key.
* ``hosts/<host>.key`` — host keys derived from it, one per simulated host
  (``xmpp-mcp-keys host-key``). This is what an agent host would be given.

Lab-only compromise: the server key and master key are world-readable so the
container's ``prosody`` user (uid 100) can read files owned by whoever runs
the tests. In production both are owned by prosody, mode 0600.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from xmpp_mcp import credentials

LAB_DIR = Path(__file__).resolve().parents[1] / "docker" / "prosody"
GENERATED = LAB_DIR / "generated"
COMPOSE = LAB_DIR / "docker-compose.yml"
CONTAINER = "xmpp-mcp-prosody-1"

HUMAN_DOMAIN = "xmpp.test"
AGENT_DOMAIN = "agents.xmpp.test"
MUC_SERVICE = "conference.xmpp.test"
HUMANS = {"alice": "alicepw", "bob": "bobpw", "carol": "carolpw"}
# Simulated agent hosts, each with its own key. "revokedhost" is listed in
# xmpp_mcp_revoked_hosts: a valid key that must nevertheless be refused.
HOSTS = ("lab", "otherhost", "revokedhost")


@dataclass
class ProsodyLab:
    host: str = "127.0.0.1"
    port: int = 5322
    ca_file: Path = GENERATED / "certs" / "ca.crt"
    host_keys: dict[str, Path] = field(
        default_factory=lambda: {h: GENERATED / "hosts" / f"{h}.key" for h in HOSTS}
    )


def _run(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True, **kw)


def _make_certs(certs: Path) -> None:
    if (certs / "lab.crt").exists():
        return
    certs.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt = certs / "ca.key", certs / "ca.crt"
    _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
         "-subj", "/CN=xmpp-mcp lab CA", "-keyout", str(ca_key), "-out", str(ca_crt))
    key, csr, crt = certs / "lab.key", certs / "lab.csr", certs / "lab.crt"
    ext = certs / "lab.ext"
    ext.write_text(
        "subjectAltName = DNS:xmpp.test, DNS:agents.xmpp.test, DNS:conference.xmpp.test\n"
        "extendedKeyUsage = serverAuth\n"
    )
    _run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=xmpp.test",
         "-keyout", str(key), "-out", str(csr))
    _run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
         "-CAcreateserial", "-days", "3650", "-extfile", str(ext), "-out", str(crt))
    os.chmod(key, 0o644)  # lab only: readable by the container's prosody user


def _make_keys() -> None:
    secrets_dir, hosts_dir = GENERATED / "secrets", GENERATED / "hosts"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    hosts_dir.mkdir(parents=True, exist_ok=True)
    master = secrets_dir / "master.key"
    if not master.exists():
        credentials.main(["new-master", "-o", str(master)])
        os.chmod(master, 0o644)  # lab only, see module docstring
    for host in HOSTS:
        path = hosts_dir / f"{host}.key"
        if not path.exists():
            credentials.main(["host-key", "--master", str(master), "--host", host,
                              "-o", str(path)])


def _wait_healthy(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(["docker", "inspect", CONTAINER, "--format",
                            "{{.State.Health.Status}}"], capture_output=True, text=True)
        if r.stdout.strip() == "healthy":
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"{CONTAINER} never became healthy")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 5322), timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Prosody c2s port 5322 never opened")


def _register_humans() -> None:
    for user, password in HUMANS.items():
        # prosodyctl register is idempotent: it overwrites the password.
        _run("docker", "exec", CONTAINER, "prosodyctl", "register", user, HUMAN_DOMAIN, password)


def container_running() -> bool:
    r = subprocess.run(["docker", "inspect", CONTAINER, "--format", "{{.State.Running}}"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "true"


def up() -> ProsodyLab:
    """Generate material if needed, start the container, create the human accounts."""
    _make_certs(GENERATED / "certs")
    _make_keys()
    _run("docker", "compose", "-f", str(COMPOSE), "up", "-d")
    _wait_healthy()
    _register_humans()
    return ProsodyLab()


def restart() -> None:
    _run("docker", "restart", CONTAINER)
    _wait_healthy()


def down() -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "down", "-v"],
                   capture_output=True, check=False)
