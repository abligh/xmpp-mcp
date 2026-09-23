"""Start the Prosody lab: humans and agents, derived credentials, TLS required.

The production-shaped lab. Compared with the ejabberd lab:

* agents live on their own virtual host (agents.xmpp.test) and log in with
  host-scoped derived credentials — no accounts, no registration, one secret
  per host (see src/xmpp_mcp/credentials.py);
* humans live on xmpp.test with ordinary password accounts;
* TLS is mandatory and clients verify it against a throwaway lab CA
  (XMPP_CA_FILE), instead of switching verification off.

It listens on 127.0.0.1:5322, so it can run alongside the Openfire or
ejabberd lab.

Usage:

    python start-lab-prosody.py

Tear down:

    docker compose -f tests/integration/docker/prosody/docker-compose.yml down -v
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
sys.path.insert(0, str(HERE / "tests"))

from integration.helpers import prosody_lab  # noqa: E402


def main() -> None:
    print("Starting the Prosody lab (first run also creates the CA, certificate and keys)…")
    lab = prosody_lab.up()
    host_key = lab.host_keys["lab"]
    print()
    print("Lab ready (Prosody 13, TLS required).")
    print(f"  XMPP:     {lab.host}:{lab.port}   CA: {lab.ca_file}")
    print(f"  Humans:   alice / bob / carol @{prosody_lab.HUMAN_DOMAIN} (password <name>pw)")
    print(f"  Agents:   <session>.<host>@{prosody_lab.AGENT_DOMAIN}, derived credentials")
    print(f"  Host keys: {', '.join(str(p) for p in lab.host_keys.values())}")
    print()
    print("An agent on host 'lab' (e.g. in .mcp.json env):")
    print(f"  XMPP_JID={{session}}.{{host}}@{prosody_lab.AGENT_DOMAIN}")
    print("  XMPP_AGENT_HOST=lab")
    print(f"  XMPP_HOST_KEY_FILE={host_key}")
    print(f"  XMPP_CA_FILE={lab.ca_file}")
    print(f"  XMPP_HOST={lab.host}   XMPP_PORT={lab.port}")
    print(f"  XMPP_CHANNEL_ALLOW=*@{prosody_lab.AGENT_DOMAIN},*@{prosody_lab.HUMAN_DOMAIN}")
    print()
    print("Run the agent suites against it:  pytest -m agents --xmpp-lab prosody")


if __name__ == "__main__":
    main()
