"""The signing flow `none`: nobody signs as the tenant, the platform does
(`registry_api.custody`)."""

import json
from typing import Any

from httpx import AsyncClient

from registry_api import webvh
from registry_api.vault.memory import MemoryVault
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.signing import link_wallet, ready, sign
from tests.test_directory import _sign_in

Headers = dict[str, str]


async def _none(client: AsyncClient, headers: Headers, tenant: str) -> dict[str, Any]:
    changed = await client.patch(
        f"/api/v1/tenants/{tenant}", json={"signing_flow": "none"}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    body: dict[str, Any] = changed.json()
    return body


async def _log(client: AsyncClient, did: str) -> list[dict[str, Any]]:
    slug = did.rsplit(":", 1)[1]
    text = (await client.get(f"/ids/{slug}/did.jsonl")).text
    return [json.loads(line) for line in text.splitlines() if line]


async def test_the_platform_signs_what_the_tenant_makes(
    client: AsyncClient, outbox: Outbox, vault: MemoryVault
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    detail = await _none(client, ada, tenant)
    assert detail["signing_flow"] == "none" and detail["signer"] is None
    # Admins still decide what is published.
    assert detail["signs"] is True
    # The key is in the vault, and the tenant's DID is signed with it at once.
    assert f"tenants/{tenant}/signing" in vault.secrets
    own = detail["identity"]["did"]
    assert own is not None
    entries = await _log(client, own)
    key = entries[0]["parameters"]["updateKeys"]
    assert len(key) == 1 and key[0].startswith("z6Mk")
    assert entries[0]["proof"][0]["verificationMethod"] == f"did:key:{key[0]}#{key[0]}"

    # A new issuer is signed when it is made, with its messaging key: no wallet.
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    got = (await client.get(f"{base}/issuers/{issuer['id']}", headers=ada)).json()
    assert got["did"] is not None and got["signature"] == "signed"
    assert got["document"]["keyAgreement"]

    # Changes to it are signed as they are made.
    await client.patch(f"{base}/issuers/{issuer['id']}", json={"mediator_id": None}, headers=ada)
    assert (await client.get(f"{base}/issuers/{issuer['id']}", headers=ada)).json()[
        "signature"
    ] == "signed"
    assert (await client.get(f"{base}/pending", headers=ada)).json()[0]["kind"] != "identity"

    # Publishing endorses it at once, signed by the platform.
    published = await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=ada)
    assert published.status_code == 200 and published.json() == {"status": "signed"}
    slug = got["did"].rsplit(":", 1)[1]
    vp = (await client.get(f"/ids/{slug}/whois.vp")).json()
    unsigned = {k: v for k, v in vp.items() if k != "proof"}
    webvh.check_proof(unsigned, vp["proof"], key, controller=own, purpose="authentication")
    assert (await client.get(f"{base}/issuers/{issuer['id']}", headers=ada)).json()[
        "published_at"
    ] is not None

    # Its health needs no wallet.
    health = (await client.get(f"{base}/health", headers=ada)).json()
    assert {c["check"]: c["issue"] for c in health["checks"]}["signing_flow"] is None


async def test_members_do_not_publish_under_none(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    await _none(client, ada, tenant)
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    denied = await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=bob)
    assert denied.status_code == 403 and denied.json()["detail"] == "not_a_signer"


async def test_wallets_hand_over_to_the_platform_and_back(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, ada, tenant)
    own = (await client.get(base, headers=ada)).json()["identity"]

    # The log names the admin's wallet: moving to `none` is signed by it.
    await _none(client, ada, tenant)
    assert (await client.get(f"{base}/identities/{own['id']}", headers=ada)).json()[
        "signature"
    ] == "outdated"
    request = await sign(client, ada, tenant, own["id"], wallet)
    platform = request["sign"]["document"]["parameters"]["updateKeys"]
    assert platform != [wallet.did.removeprefix("did:key:")]

    # From then on the platform signs, and hands back by itself.
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    assert (await client.get(f"{base}/issuers/{issuer['id']}", headers=ada)).json()[
        "signature"
    ] == "signed"
    back = await client.patch(base, json={"signing_flow": "any_admin"}, headers=ada)
    assert back.status_code == 200
    entries = await _log(client, own["did"])
    assert entries[-1]["parameters"]["updateKeys"] == [wallet.did.removeprefix("did:key:")]
    assert entries[-1]["proof"][0]["verificationMethod"].startswith(f"did:key:{platform[0]}")
    # And wallets sign again.
    asked = await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=ada)
    assert "request_uri" in asked.json()


async def test_a_signer_who_is_not_an_admin_hands_over(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    bob_id = (await client.get("/api/v1/auth/me", headers=bob)).json()["id"]
    await client.patch(base, json={"signing_flow": "single_user", "signer_id": bob_id}, headers=ada)
    wallet = FakeWallet()
    await link_wallet(client, bob, wallet)
    own = (await client.get(base, headers=ada)).json()["identity"]
    await sign(client, bob, tenant, own["id"], wallet)

    await _none(client, ada, tenant)
    # Bob signs no more as the tenant, but his key is the one in force.
    request = await sign(client, bob, tenant, own["id"], wallet)
    assert request["sign"]["kind"] == "did_log_entry"
    assert (await client.get(f"{base}/identities/{own['id']}", headers=ada)).json()[
        "signature"
    ] == "signed"
