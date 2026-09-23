"""Start the ejabberd lab and prime it for Claude Code use.

Same shape as start-lab.py, different XMPP server: ejabberd 25.04 instead of
Openfire. The key behavioural difference is that **MAM (XEP-0313) MUC
queries actually answer** — meaning ``mam_query`` returns historical room
messages across MCP-server restarts.

Trade-offs vs the Openfire lab:

* ✅ MAM works (the headline)
* ✅ Same domain / accounts / ports — every existing script/test that
  doesn't depend on Openfire REST works unchanged
* ❌ The Openfire ``of_*`` admin tools fail (no Openfire REST API to talk to)
* ❌ The XEP-0258 stub component fixture's auth differs slightly — see
  CLAUDE.md

Usage:

    python start-lab-ejabberd.py

Tear down:

    docker compose -f tests/integration/docker/ejabberd/docker-compose.yml down -v
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "tests" / "integration" / "docker" / "ejabberd" / "docker-compose.yml"
CONTAINER = "xmpp-mcp-ej-1"

DOMAIN = "xmpp.test"
ACCOUNTS = {
    "bot":   "botpw",
    "alice": "alicepw",
    "bob":   "bobpw",
    "carol": "carolpw",
    "admin": "adminpw",
    # The global webhook-to-XMPP relay (scripts/webhook_relay.py) logs in as this.
    "webhook": "webhookpw",
}
# r1-r3 are the test rooms; "agents" is the shared directory room every
# channel-mode agent joins (XMPP_AUTO_JOIN) so list_agents sees the whole fleet.
ROOMS = ("r1", "r2", "r3", "agents")
MUC_SERVICE = f"conference.{DOMAIN}"


def step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}", flush=True)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def ejabberdctl(*args: str) -> subprocess.CompletedProcess:
    """Run an ejabberdctl command inside the running container."""
    return subprocess.run(
        ["docker", "exec", CONTAINER, "ejabberdctl", *args],
        check=False, capture_output=True, text=True,
    )


def wait_for_health(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = docker("inspect", CONTAINER, "--format", "{{.State.Health.Status}}", check=False)
        if r.stdout.strip() == "healthy":
            return
        time.sleep(1.0)
    raise SystemExit(f"container {CONTAINER} never went healthy")


def wait_for_tcp(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"timeout waiting for {host}:{port}")


def register_users() -> None:
    for name, password in ACCOUNTS.items():
        r = ejabberdctl("register", name, DOMAIN, password)
        out = (r.stdout + r.stderr).strip().lower()
        if "successfully registered" in out:
            print(f"      + {name}@{DOMAIN}")
        elif "already registered" in out:
            print(f"      = {name}@{DOMAIN} (already exists)")
        else:
            raise SystemExit(
                f"register {name}@{DOMAIN} failed (rc={r.returncode}): "
                f"{r.stdout!r} {r.stderr!r}"
            )


def create_rooms() -> None:
    """Pre-create the persistent MUC rooms `r1`/`r2`/`r3` with MAM on."""
    for name in ROOMS:
        # `create_room` sub-command: room, service, host
        r = ejabberdctl("create_room", name, MUC_SERVICE, DOMAIN)
        out = (r.stdout + r.stderr).strip().lower()
        if "created" in out or r.returncode == 0:
            print(f"      + {name}@{MUC_SERVICE}")
        elif "already" in out or "exists" in out:
            print(f"      = {name}@{MUC_SERVICE} (already exists)")
        else:
            raise SystemExit(
                f"create_room {name} failed (rc={r.returncode}): "
                f"{r.stdout!r} {r.stderr!r}"
            )


def main() -> None:
    if not COMPOSE.exists():
        raise SystemExit(f"missing compose file: {COMPOSE}")

    step(1, 5, "docker compose up -d")
    docker("compose", "-f", str(COMPOSE), "up", "-d")

    step(2, 5, "waiting for container health")
    wait_for_health()

    step(3, 5, f"waiting for XMPP C2S (127.0.0.1:5222)")
    wait_for_tcp("127.0.0.1", 5222)

    step(4, 5, "registering accounts")
    register_users()

    step(5, 5, f"creating MUC rooms ({', '.join(ROOMS)} on {MUC_SERVICE})")
    create_rooms()

    print()
    print("Lab ready (ejabberd 25.04 + MAM).")
    print(f"  XMPP:        127.0.0.1:5222  (bot@{DOMAIN} / botpw)")
    print(f"  HTTP admin:  http://127.0.0.1:5280/admin  (admin@{DOMAIN} / adminpw)")
    print()
    print("Start Claude Code in this directory:  claude")
    print("(MAM queries via the mam_query tool now actually answer)")
    print()
    print("Agents: accounts self-register on first login (XMPP_REGISTER=true);")
    print(f"        shared directory room is agents@{MUC_SERVICE}")


if __name__ == "__main__":
    main()
