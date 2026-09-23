"""Integration test fixtures.

Boots Openfire 4.8.1 once per pytest session via docker compose, primes the
REST API plugin (which is disabled by default — see the long comment in
``_enable_restapi``), and exposes a fresh in-process FastMCP client to each
test.

These fixtures only fire for tests marked ``@pytest.mark.docker``.
"""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastmcp import Client

from xmpp_mcp.server import create_server

from .helpers.raw_client import RawXMPPClient

_COMPOSE_DIR = Path(__file__).parent / "docker"

# Nick the test bot joins MUCs under. Pinned by the fixtures so the suite is
# hermetic — without this, pydantic-settings would read XMPP_NICK from a
# developer's local .env and MUC assertions would vary per machine.
BOT_NICK = "xmpp-mcp"


@dataclass(frozen=True)
class Account:
    username: str
    password: str

    @property
    def jid(self) -> str:
        return f"{self.username}@xmpp.test"


@dataclass
class OpenfireHandle:
    host: str = "127.0.0.1"
    c2s_port: int = 5222
    rest_port: int = 9090
    domain: str = "xmpp.test"
    muc_service: str = "conference.xmpp.test"
    admin_user: str = "admin"
    admin_password: str = "adminpw"
    accounts: dict[str, Account] = field(
        default_factory=lambda: {
            "bot": Account("bot", "botpw"),
            "alice": Account("alice", "alicepw"),
            "bob": Account("bob", "bobpw"),
            "carol": Account("carol", "carolpw"),
        }
    )
    # Pre-created persistent MUC rooms (local part only — fully qualify via .room_jid()).
    room_names: tuple[str, ...] = ("r1", "r2", "r3")

    @property
    def admin_url(self) -> str:
        return f"http://{self.host}:{self.rest_port}"

    def room_jid(self, name: str) -> str:
        return f"{name}@{self.muc_service}"


# --- session-scoped: bring Openfire up once -------------------------------


@pytest.fixture(scope="session")
def openfire() -> Iterator[OpenfireHandle]:
    """Bring Openfire up for the whole test session and tear it down at exit."""
    subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=_COMPOSE_DIR, check=True, capture_output=True,
    )
    handle = OpenfireHandle()
    try:
        _wait_for_tcp(handle.host, handle.c2s_port, timeout=120)
        _wait_for_http(f"{handle.admin_url}/login.jsp", timeout=120)
        _enable_restapi(handle)
        _wait_for_rest_api(handle, timeout=30)
        _create_rooms(handle)
        yield handle
    finally:
        subprocess.run(
            ["docker", "compose", "down", "-v"],
            cwd=_COMPOSE_DIR, check=False, capture_output=True,
        )


# --- function-scoped: fresh MCP client per test ---------------------------


@pytest_asyncio.fixture
async def mcp(
    openfire: OpenfireHandle, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Client]:
    """A FastMCP in-process Client signed in as the `bot` account."""
    bot = openfire.accounts["bot"]
    monkeypatch.setenv("XMPP_JID", bot.jid)
    monkeypatch.setenv("XMPP_PASSWORD", bot.password)
    monkeypatch.setenv("XMPP_HOST", openfire.host)
    monkeypatch.setenv("XMPP_PORT", str(openfire.c2s_port))
    monkeypatch.setenv("XMPP_TLS_INSECURE", "true")
    monkeypatch.setenv("XMPP_NICK", BOT_NICK)  # hermetic: ignore developer .env
    monkeypatch.setenv("OPENFIRE_BASE_URL", openfire.admin_url)
    monkeypatch.setenv("OPENFIRE_ADMIN_USER", openfire.admin_user)
    monkeypatch.setenv("OPENFIRE_ADMIN_PASSWORD", openfire.admin_password)
    server = create_server()
    async with Client(server) as client:
        yield client


@pytest_asyncio.fixture
async def raw_alice(openfire: OpenfireHandle) -> AsyncIterator[RawXMPPClient]:
    """A raw slixmpp client signed in as alice — the other side of bot's chats."""
    async with _raw_for(openfire, "alice") as c:
        yield c


@pytest_asyncio.fixture
async def raw_carol(openfire: OpenfireHandle) -> AsyncIterator[RawXMPPClient]:
    """A raw slixmpp client signed in as carol — used to verify MUC delivery."""
    async with _raw_for(openfire, "carol") as c:
        yield c


@pytest_asyncio.fixture
async def raw_bob(openfire: OpenfireHandle) -> AsyncIterator[RawXMPPClient]:
    """A raw slixmpp client signed in as bob — used for multi-sender filtering tests."""
    async with _raw_for(openfire, "bob") as c:
        yield c


@pytest_asyncio.fixture
async def seclabel_component(openfire: OpenfireHandle):
    """A XEP-0258 stub component running on seclabel.xmpp.test.

    **Function-scoped** even though setup is slow (~1s): session-scoped async
    fixtures live in a different event loop than the function-scoped tests,
    so the component's socket reads stop being processed during the test —
    every IQ would time out. Keeping it function-scoped keeps it on the
    test's loop where its handler can actually answer.
    """
    from .helpers.seclabel_component import (
        DEFAULT_DOMAIN, DEFAULT_SECRET, SecurityLabelComponent,
    )

    # Wait for the component port to open — `xmpp.component.socket.active=true`
    # was set during _enable_restapi but Openfire takes a beat to bind the
    # socket.
    _wait_for_tcp(openfire.host, 5275, timeout=30)

    component = SecurityLabelComponent(
        jid=DEFAULT_DOMAIN,
        secret=DEFAULT_SECRET,
        server=openfire.host,
        port=5275,
    )
    component.connect()
    await component.wait_ready(timeout=15)
    try:
        yield DEFAULT_DOMAIN
    finally:
        try:
            await component.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _raw_for(openfire: OpenfireHandle, name: str) -> RawXMPPClient:
    acct = openfire.accounts[name]
    return RawXMPPClient(acct.jid, acct.password, openfire.host, openfire.c2s_port)


# --- helpers --------------------------------------------------------------


def _wait_for_tcp(host: str, port: int, timeout: float) -> None:
    """Block until a TCP port accepts connections (or fail with context)."""
    deadline = time.monotonic() + timeout
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(1.0)
    raise RuntimeError(f"timeout waiting for {host}:{port} — last error: {last_err}")


def _wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last: str = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(1.0)
    raise RuntimeError(f"timeout waiting for {url} — last: {last}")


def _enable_restapi(handle: OpenfireHandle) -> None:
    """Enable the REST API plugin via the admin console session.

    The plugin reads ``plugin.restapi.enabled`` from JiveGlobals.getProperty(),
    which reads only from the DB-backed system property store — XML properties
    in openfire.xml are a separate namespace and don't satisfy the check.
    Openfire ships no facility to seed DB-backed system properties at first
    boot, so we drive the admin console form here, exactly the way a human
    would. One-time per session.
    """
    with httpx.Client(base_url=handle.admin_url, follow_redirects=False) as c:
        login_page = c.get("/login.jsp")
        csrf = _extract_csrf(login_page.text, where="login page")
        r = c.post(
            "/login.jsp",
            data={
                "url": "/index.jsp",
                "login": "true",
                "csrf": csrf,
                "username": handle.admin_user,
                "password": handle.admin_password,
            },
        )
        if r.status_code not in (302, 303):
            raise RuntimeError(
                f"admin login failed: status={r.status_code} body[:200]={r.text[:200]!r}"
            )
        # Set several DB-backed system properties through the admin form:
        #   - REST API plugin enablement (see above)
        #   - External components on port 5275 (XEP-0114) — used by the
        #     XEP-0258 stub component (helpers/seclabel_component.py)
        #   - Monitoring plugin: enable message + group-chat (MUC) archiving
        #     so XEP-0313 MAM queries return historical room messages
        for key, value in (
            ("adminConsole.access.allow-wildcards-in-excludes", "true"),
            ("plugin.restapi.enabled", "true"),
            ("xmpp.component.socket.active", "true"),
            ("xmpp.component.socket.port", "5275"),
            ("xmpp.component.defaultSecret", "component-secret"),
            ("conversation.metadataArchiving", "true"),
            ("conversation.messageArchiving", "true"),
            ("conversation.roomArchiving", "true"),
            # CRITICAL for MAM: without this, the plugin stores only message
            # metadata for MUC rooms and MAM queries time out trying to
            # reconstruct stanzas. See openfire-monitoring-plugin#167.
            ("conversation.roomArchivingStanzas", "true"),
        ):
            props_page = c.get("/server-properties.jsp")
            csrf2 = _extract_csrf(props_page.text, where="server-properties page")
            r = c.post(
                "/server-properties.jsp",
                data={
                    "csrf": csrf2,
                    "action": "save",
                    "key": key,
                    "value": value,
                    "encrypt": "false",
                },
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"set-property {key!r} failed: status={r.status_code} "
                    f"body[:200]={r.text[:200]!r}"
                )


def _extract_csrf(html: str, *, where: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError(f"no csrf token found in {where}; body[:200]={html[:200]!r}")
    return m.group(1)


def _create_rooms(handle: OpenfireHandle) -> None:
    """Pre-create the persistent MUC rooms used by test_muc_e2e.py."""
    auth = (handle.admin_user, handle.admin_password)
    url = f"{handle.admin_url}/plugins/restapi/v1/chatrooms"
    for name in handle.room_names:
        body = {
            "roomName": name,
            "naturalName": f"Room {name}",
            "description": f"Integration test room {name}",
            "persistent": True,
            "publicRoom": True,
            # MAM (test_mam_e2e.py) needs server-side archiving on every room
            # the bot might want to query historically.
            "logEnabled": True,
        }
        r = httpx.post(
            url,
            json=body,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        # 201 = created. 409 = already exists (idempotent re-run after partial teardown).
        if r.status_code not in (201, 200, 409):
            raise RuntimeError(
                f"creating room {name!r} failed: {r.status_code} {r.text.strip()!r}"
            )


def _wait_for_rest_api(handle: OpenfireHandle, timeout: float) -> None:
    """Verify the REST API now returns JSON for an authenticated GET /users."""
    auth = (handle.admin_user, handle.admin_password)
    url = f"{handle.admin_url}/plugins/restapi/v1/users"
    deadline = time.monotonic() + timeout
    last: str = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                url,
                auth=auth,
                headers={"Accept": "application/json"},
                timeout=3.0,
                follow_redirects=False,
            )
            if r.status_code == 200 and "application/json" in r.headers.get(
                "content-type", ""
            ):
                return
            last = f"HTTP {r.status_code} ct={r.headers.get('content-type','?')}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(1.0)
    raise RuntimeError(f"REST API never returned JSON — last: {last}")


# --- agent labs: ejabberd or Prosody (channel / identity / relay suites) -----
#
# The `agents` suites need a server where each agent gets its own identity
# without anyone creating accounts by hand. Two labs provide that, differently:
#
# * ejabberd (default): open XEP-0077 registration + one shared password,
#   plain-text c2s. Simple; lab-only security.
# * Prosody (--xmpp-lab prosody): the production shape — agents on their own
#   virtual host with host-scoped derived credentials (no accounts at all),
#   humans on ordinary password accounts, TLS required and verified.
#
# The same tests run against either; a LabHandle hides the differences. The
# ejabberd lab binds the Openfire lab's ports, so its tests are dropped when
# Openfire tests are selected too; Prosody (port 5322) coexists with both.


class LabHandle:
    """What the agent suites need from a lab."""

    name: str
    host = "127.0.0.1"
    c2s_port: int
    domain = "xmpp.test"               # humans
    agent_domain: str                  # agents
    muc_service = "conference.xmpp.test"
    humans = ("alice", "bob", "carol")

    def room_jid(self, name: str) -> str:
        return f"{name}@{self.muc_service}"

    def raw(self, name: str) -> RawXMPPClient:
        """A human's plain XMPP client (the other side of conversations)."""
        return RawXMPPClient(f"{name}@{self.domain}", f"{name}pw", self.host, self.c2s_port)

    def agent_env(self) -> dict[str, str]:
        """Environment for an xmpp-mcp agent in this lab (JID template left to the caller)."""
        raise NotImplementedError

    def agent_cli(self) -> list[str]:
        return []

    @property
    def relay_jid(self) -> str:
        raise NotImplementedError

    def relay_settings(self, **kw: object):
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError


_EJ_COMPOSE = _COMPOSE_DIR / "ejabberd" / "docker-compose.yml"
_EJ_CONTAINER = "xmpp-mcp-ej-1"


class EjabberdHandle(LabHandle):
    name = "ejabberd"
    c2s_port = 5222
    agent_domain = "xmpp.test"
    container = _EJ_CONTAINER
    # Every agent account self-registers (XEP-0077) with this password.
    agent_password = "agentpw"
    accounts = {n: Account(n, f"{n}pw") for n in ("alice", "bob", "carol", "webhook")}

    def agent_env(self) -> dict[str, str]:
        return {
            "XMPP_PASSWORD": self.agent_password,
            "XMPP_HOST": self.host,
            "XMPP_PORT": str(self.c2s_port),
            "XMPP_TLS_INSECURE": "true",
        }

    def agent_cli(self) -> list[str]:
        return ["--register"]

    @property
    def relay_jid(self) -> str:
        return "webhook@xmpp.test"

    def relay_settings(self, **kw: object):
        from xmpp_mcp.webhook_relay import RelaySettings

        return RelaySettings(  # type: ignore[call-arg]
            _env_file=None, xmpp_jid=self.relay_jid, xmpp_password="webhookpw",
            xmpp_host=self.host, xmpp_port=self.c2s_port, xmpp_tls_insecure=True, **kw,
        )

    def restart(self) -> None:
        subprocess.run(["docker", "restart", self.container], check=True, capture_output=True)
        _wait_for_ejabberd(self)


class ProsodyHandle(LabHandle):
    name = "prosody"
    c2s_port = 5322
    agent_domain = "agents.xmpp.test"

    def __init__(self, lab) -> None:  # helpers.prosody_lab.ProsodyLab
        self.lab = lab

    def agent_env(self) -> dict[str, str]:
        return {
            # No password: derived per connect from this host's key.
            "XMPP_HOST_KEY_FILE": str(self.lab.host_keys["lab"]),
            "XMPP_CA_FILE": str(self.lab.ca_file),  # TLS verified, not skipped
            "XMPP_HOST": self.host,
            "XMPP_PORT": str(self.c2s_port),
            # Humans live on the parent domain; let them through the gate.
            "XMPP_CHANNEL_ALLOW": f"*@{self.agent_domain},*@{self.domain}",
        }

    @property
    def relay_jid(self) -> str:
        return f"webhook.lab@{self.agent_domain}"

    def relay_settings(self, **kw: object):
        from xmpp_mcp.webhook_relay import RelaySettings

        return RelaySettings(  # type: ignore[call-arg]
            _env_file=None, xmpp_jid=self.relay_jid,
            xmpp_host_key_file=str(self.lab.host_keys["lab"]),
            xmpp_ca_file=str(self.lab.ca_file),
            xmpp_host=self.host, xmpp_port=self.c2s_port, **kw,
        )

    def restart(self) -> None:
        from .helpers import prosody_lab

        prosody_lab.restart()


@pytest.hookimpl(trylast=True)  # after -m / -k have deselected what they will
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--xmpp-lab") != "ejabberd":
        return  # Prosody listens on its own port: no clash with Openfire
    agents = [i for i in items if i.get_closest_marker("agents")]
    openfire_docker = [
        i for i in items if i.get_closest_marker("docker") and not i.get_closest_marker("agents")
    ]
    if agents and openfire_docker:
        config.hook.pytest_deselected(items=agents)
        items[:] = [i for i in items if not i.get_closest_marker("agents")]


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", name, "--format", "{{.State.Running}}"],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() == "true"


def _wait_for_ejabberd(handle: EjabberdHandle, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "inspect", handle.container, "--format", "{{.State.Health.Status}}"],
            capture_output=True, text=True, check=False,
        )
        if r.stdout.strip() == "healthy":
            _wait_for_tcp(handle.host, handle.c2s_port, timeout=60)
            return
        time.sleep(1.0)
    raise RuntimeError(f"{handle.container} never became healthy")


def _ejabberd_lab() -> Iterator[EjabberdHandle]:
    handle = EjabberdHandle()
    started_here = not _container_running(handle.container)
    subprocess.run(
        ["docker", "compose", "-f", str(_EJ_COMPOSE), "up", "-d"],
        check=True, capture_output=True,
    )
    try:
        _wait_for_ejabberd(handle)
        for acct in handle.accounts.values():
            # Idempotent: "already registered" is fine.
            subprocess.run(
                ["docker", "exec", handle.container, "ejabberdctl", "register",
                 acct.username, handle.domain, acct.password],
                capture_output=True, check=False,
            )
        yield handle
    finally:
        if started_here:
            subprocess.run(
                ["docker", "compose", "-f", str(_EJ_COMPOSE), "down", "-v"],
                check=False, capture_output=True,
            )


def _prosody_lab() -> Iterator[ProsodyHandle]:
    from .helpers import prosody_lab

    started_here = not prosody_lab.container_running()
    try:
        yield ProsodyHandle(prosody_lab.up())
    finally:
        if started_here:
            prosody_lab.down()


@pytest.fixture(scope="session")
def lab(request: pytest.FixtureRequest) -> Iterator[LabHandle]:
    """The agent lab chosen by --xmpp-lab: reused if already running, else booted."""
    which = request.config.getoption("--xmpp-lab")
    yield from (_prosody_lab() if which == "prosody" else _ejabberd_lab())


@pytest_asyncio.fixture
async def spawn_agent(lab: LabHandle, tmp_path: Path):
    """Factory: start a channel-mode xmpp-mcp subprocess as a fresh agent.

    ``await spawn_agent("rev")`` → a started :class:`StdioMCP` whose JID is
    ``rev-<random>.lab@<agent domain>``, with whatever credentials the lab
    uses (a self-registered shared password on ejabberd, a key-derived one on
    Prosody). Extra CLI args / env can be passed; ``channel=False`` starts it
    in plain mode.

    ``claude_name="Reviewer"`` instead runs the agent as if under a Claude
    Code session of that name: it writes a session file into a private
    CLAUDE_CONFIG_DIR and uses the canonical ``{session}.{host}`` JID. The
    returned object's ``session_file`` can be edited to rename the session.
    Every agent gets its own config dir, so none of them ever sees the Claude
    Code session this test suite may itself be running under.

    All agents are shut down at the end of the test.
    """
    import json
    import uuid

    from .helpers.stdio_mcp import StdioMCP

    started: list[StdioMCP] = []

    async def _spawn(
        base: str,
        *args: str,
        channel: bool = True,
        env: dict[str, str] | None = None,
        name: str | None = None,
        claude_name: str | None = None,
    ) -> StdioMCP:
        agent_name = name or f"{base}-{uuid.uuid4().hex[:6]}"
        config_dir = tmp_path / f"claude-{agent_name}"
        (config_dir / "sessions").mkdir(parents=True, exist_ok=True)
        agent_env = {
            **lab.agent_env(),
            "XMPP_JID": "{agent}.{host}@" + lab.agent_domain,
            "XMPP_AGENT_HOST": "lab",
            "XMPP_AGENT_ID": f"session-{agent_name}",
            "CLAUDE_CONFIG_DIR": str(config_dir),
        }
        cli = lab.agent_cli()
        session_file = None
        if claude_name is None:
            cli += ["--agent-name", agent_name]
            jid = f"{agent_name.lower()}.lab@{lab.agent_domain}"
        else:
            session_id = str(uuid.uuid4())
            session_file = config_dir / "sessions" / "424242.json"
            session_file.write_text(json.dumps({
                "pid": 424242, "sessionId": session_id, "name": claude_name,
                "nameSource": "auto", "status": "idle",
            }))
            del agent_env["XMPP_AGENT_ID"]
            agent_env.update({
                "XMPP_JID": "{session}.{host}@" + lab.agent_domain,
                "CLAUDE_CODE_SESSION_ID": session_id,  # found by the session-ID scan
                "XMPP_CLAUDE_SESSION_POLL": "0.5",
            })
            jid = f"{session_id}.lab@{lab.agent_domain}"
            agent_name = claude_name
        if channel:
            cli.append("--channel")
        proc = StdioMCP([*cli, *args], env={**agent_env, **(env or {})},
                        cwd=tmp_path)  # cwd: no stray .env
        proc.agent_name = agent_name  # type: ignore[attr-defined]
        proc.jid = jid  # type: ignore[attr-defined]
        proc.session_file = session_file  # type: ignore[attr-defined]
        await proc.start()
        started.append(proc)
        return proc

    try:
        yield _spawn
    finally:
        for proc in started:
            await proc.close()
