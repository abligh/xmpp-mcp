"""The Prosody lab's authentication, attacked directly (pytest -m agents --xmpp-lab prosody).

The client refuses to mint a credential for another host's JID — but the
security of the scheme rests on the *server* refusing, so these tests talk to
Prosody with hand-made credentials and a bare slixmpp client, bypassing
xmpp-mcp entirely. TLS is verified against the lab CA throughout, except
where the point is to go without it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from slixmpp import ClientXMPP

from xmpp_mcp.credentials import load_host_key, mint_password

from .conftest import LabHandle

pytestmark = [pytest.mark.docker, pytest.mark.agents]


@pytest.fixture
def prosody(lab: LabHandle) -> LabHandle:
    if lab.name != "prosody":
        pytest.skip("derived-credential auth is what the Prosody lab adds (--xmpp-lab prosody)")
    return lab


async def _login(lab: LabHandle, jid: str, password: str, *, tls: bool = True) -> bool:
    """True if the server accepts this JID/password (and binds a session)."""
    xmpp = ClientXMPP(jid, password)
    xmpp.enable_direct_tls = False
    xmpp.ssl_context.load_verify_locations(cafile=str(lab.lab.ca_file))
    if not tls:
        # Refuse STARTTLS and allow SASL in the clear: does the server accept that?
        xmpp.enable_starttls = False
        xmpp["feature_mechanisms"].unencrypted_plain = True
        xmpp["feature_mechanisms"].unencrypted_scram = True
    result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def done(ok: bool) -> Any:
        def handler(_event: Any) -> None:
            if not result.done():
                result.set_result(ok)
        return handler

    xmpp.add_event_handler("session_start", done(True))
    xmpp.add_event_handler("failed_all_auth", done(False))
    xmpp.add_event_handler("disconnected", done(False))
    xmpp.connect(host=lab.host, port=lab.c2s_port)
    try:
        return await asyncio.wait_for(result, timeout=15)
    except asyncio.TimeoutError:
        return False
    finally:
        xmpp.cancel_connection_attempt()
        try:
            await asyncio.wait_for(xmpp.disconnect(), timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _agent_jid(host: str = "lab", domain: str = "agents.xmpp.test") -> str:
    return f"{uuid.uuid4()}.{host}@{domain}"


async def test_a_derived_credential_logs_in(prosody: LabHandle) -> None:
    jid = _agent_jid()
    key = load_host_key(prosody.lab.host_keys["lab"])
    assert await _login(prosody, jid, key.password_for(jid))


async def test_each_host_logs_in_as_its_own_agents(prosody: LabHandle) -> None:
    jid = _agent_jid("otherhost")
    key = load_host_key(prosody.lab.host_keys["otherhost"])
    assert await _login(prosody, jid, key.password_for(jid))


async def test_one_host_cannot_impersonate_another(prosody: LabHandle) -> None:
    """otherhost's key, a lab JID — minted directly, bypassing the client's check."""
    jid = _agent_jid("lab")
    other = load_host_key(prosody.lab.host_keys["otherhost"])
    forged = mint_password(other.key, jid, int(time.time()) + 600)
    assert not await _login(prosody, jid, forged)


async def test_a_revoked_host_is_refused(prosody: LabHandle) -> None:
    """A correctly derived credential, from a host the operator has revoked."""
    jid = _agent_jid("revokedhost")
    key = load_host_key(prosody.lab.host_keys["revokedhost"])
    assert not await _login(prosody, jid, key.password_for(jid))


async def test_an_expired_credential_is_refused(prosody: LabHandle) -> None:
    jid = _agent_jid()
    key = load_host_key(prosody.lab.host_keys["lab"])
    stale = mint_password(key.key, jid, int(time.time()) - 3600)
    assert not await _login(prosody, jid, stale)


async def test_an_over_long_lifetime_is_refused(prosody: LabHandle) -> None:
    jid = _agent_jid()
    key = load_host_key(prosody.lab.host_keys["lab"])
    forever = mint_password(key.key, jid, int(time.time()) + 365 * 86400)
    assert not await _login(prosody, jid, forever)


async def test_a_credential_only_works_for_its_own_jid(prosody: LabHandle) -> None:
    key = load_host_key(prosody.lab.host_keys["lab"])
    mine, yours = _agent_jid(), _agent_jid()
    assert not await _login(prosody, yours, key.password_for(mine))


async def test_agents_cannot_use_passwords(prosody: LabHandle) -> None:
    """The agents host has no stored accounts: only derived credentials work."""
    assert not await _login(prosody, _agent_jid(), "alicepw")


async def test_humans_log_in_with_ordinary_passwords(prosody: LabHandle) -> None:
    assert await _login(prosody, "alice@xmpp.test", "alicepw")
    assert not await _login(prosody, "alice@xmpp.test", "wrong")


async def test_derived_credentials_do_not_work_for_humans(prosody: LabHandle) -> None:
    """The human host authenticates against real accounts, not host keys."""
    jid = _agent_jid(domain="xmpp.test")
    key = load_host_key(prosody.lab.host_keys["lab"])
    assert not await _login(prosody, jid, key.password_for(jid))


async def test_no_login_without_tls(prosody: LabHandle) -> None:
    """The credential travels as SASL PLAIN, so the server must insist on TLS."""
    jid = _agent_jid()
    key = load_host_key(prosody.lab.host_keys["lab"])
    assert not await _login(prosody, jid, key.password_for(jid), tls=False)
    assert not await _login(prosody, "alice@xmpp.test", "alicepw", tls=False)


async def test_an_agent_and_a_human_talk(prosody: LabHandle, spawn_agent) -> None:
    """Across the two virtual hosts: the whole point of splitting them."""
    agent = await spawn_agent("mixed")
    async with prosody.raw("alice") as alice:
        alice.send_chat(agent.jid, "hello from a human")
        ev = await agent.next_event()
        assert (ev["content"], ev["meta"]["sender_jid"]) == ("hello from a human", "alice@xmpp.test")
        await agent.call("reply", {"to": ev["meta"]["reply_to"], "message": "hello, human"})
        assert (await alice.wait_for_message(timeout=5)).body == "hello, human"
