import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import new_mediator
from tests.signing import link_wallet, ready, sign
from tests.test_directory import _sign_in


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_one_member_signs(client: AsyncClient, outbox: Outbox, kind: str) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    item = (await client.post(f"{base}/{kind}", json={"name": "Uni"}, headers=ada)).json()
    url = f"{base}/{kind}/{item['id']}/signing"

    assert (await client.get(url, headers=ada)).json() == {"system": None, "signer": None}

    members = (await client.get(f"{base}/members", headers=ada)).json()
    bob_id = next(m["user_id"] for m in members if m["email"] == "bob@example.org")
    body = {"system": "single_user", "user_id": bob_id}
    set_ = await client.put(url, json=body, headers=ada)
    assert set_.status_code == 200, set_.text
    assert set_.json()["system"] == "single_user"
    assert set_.json()["signer"]["email"] == "bob@example.org"
    assert set_.json()["signer"]["member"] is True
    # Any member reads it; only admins set it.
    assert (await client.get(url, headers=bob)).json() == set_.json()
    denied = await client.put(url, json=body, headers=bob)
    assert denied.status_code == 403

    missing = await client.put(url, json={"system": "single_user"}, headers=ada)
    assert missing.status_code == 422 and missing.json()["detail"] == "signer_required"
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    outsider = {"system": "single_user", "user_id": str(uuid.uuid4())}
    stranger = await client.put(url, json=outsider, headers=ada)
    assert stranger.status_code == 422 and stranger.json()["detail"] == "signer_not_member"
    assert (await client.get(url, headers=eve)).status_code == 404

    cleared = await client.put(url, json={"system": None}, headers=ada)
    assert cleared.json() == {"system": None, "signer": None}


async def test_mediators_have_no_signing(client: AsyncClient, outbox: Outbox, dns: Dns) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    mediator = await new_mediator(client, ada, tenant, dns)
    response = await client.get(f"{base}/mediators/{mediator['id']}/signing", headers=ada)
    assert response.status_code in (404, 422)


async def test_the_signers_key_goes_in_the_document(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    admin_wallet = await ready(client, ada, tenant)
    await client.post(
        f"{base}/invitations", json={"email": "bob@example.org", "role": "member"}, headers=ada
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    identity_url = f"{base}/identities/{issuer['identity']['id']}"
    await sign(client, ada, tenant, issuer["identity"]["id"], admin_wallet)

    # Bob signs for it, but has no wallet yet: no key to publish.
    members = (await client.get(f"{base}/members", headers=ada)).json()
    bob_id = next(m["user_id"] for m in members if m["email"] == "bob@example.org")
    body = {"system": "single_user", "user_id": bob_id}
    signing = await client.put(f"{base}/issuers/{issuer['id']}/signing", json=body, headers=ada)
    assert signing.json()["signer"]["wallet"] is False
    detail = (await client.get(identity_url, headers=ada)).json()
    assert "assertionMethod" not in detail["document"]
    assert detail["signature"] == "signed"

    # He links one: the document should now name his key, and waits to be signed.
    bob_wallet = FakeWallet()
    await link_wallet(client, bob, bob_wallet)
    detail = (await client.get(identity_url, headers=ada)).json()
    assert detail["signature"] == "outdated"
    did = detail["did"]
    key = bob_wallet.did.removeprefix("did:key:")
    agreement = detail["document"]["keyAgreement"][0].removeprefix(f"{did}#")
    assert detail["document"]["verificationMethod"] == [
        {"id": f"{did}#{key}", "type": "Multikey", "controller": did, "publicKeyMultibase": key},
        {
            "id": f"{did}#{agreement}",
            "type": "Multikey",
            "controller": did,
            "publicKeyMultibase": agreement,
        },
    ]
    assert detail["document"]["assertionMethod"] == [f"{did}#{key}"]
    await sign(client, ada, tenant, issuer["identity"]["id"], admin_wallet)
    assert (await client.get(identity_url, headers=ada)).json()["signature"] == "signed"

    # The tenant's own identity is signed for by its admins' wallets.
    own = (await client.get(base, headers=ada)).json()["identity"]["id"]
    tenant_doc = (await client.get(f"{base}/identities/{own}", headers=ada)).json()["document"]
    admin_key = admin_wallet.did.removeprefix("did:key:")
    assert tenant_doc["assertionMethod"] == [f"{tenant_doc['id']}#{admin_key}"]
    # Bob is a member, not an admin: his wallet signs nothing for the tenant.
    assert all(key not in method for method in tenant_doc["assertionMethod"])
