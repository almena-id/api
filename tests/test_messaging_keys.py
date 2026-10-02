"""Issuers' and verifiers' messaging keys: made when their signer signs them,
kept in the vault, named by their DID document only from then on."""

import base64

import pytest
from httpx import AsyncClient

from registry_api.main import app
from registry_api.messaging_keys import multikey
from registry_api.vault import VaultError, get_vault
from registry_api.vault.memory import MemoryVault
from tests.conftest import Dns, Outbox
from tests.mediators import new_mediator
from tests.signing import publish, ready, sign


class _DownVault(MemoryVault):
    async def read(self, path: str) -> dict[str, str] | None:
        raise VaultError("down")


async def _sign_in(client: AsyncClient, outbox: Outbox) -> tuple[dict[str, str], str]:
    await client.post("/api/v1/auth/code", json={"email": "ada@example.org"})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": "ada@example.org", "code": outbox.last_code()}
    )
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    tenant = (await client.get("/api/v1/tenants", headers=headers)).json()[0]["id"]
    return headers, tenant


def _x(jwk: dict[str, str]) -> bytes:
    return base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_the_key_is_made_when_its_signer_signs(
    client: AsyncClient, outbox: Outbox, dns: Dns, vault: MemoryVault, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox)
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    await publish(client, headers, tenant, "mediators", mediator["id"], wallet)
    item = (await client.post(f"{base}/{kind}", json={"name": "Uni"}, headers=headers)).json()
    await client.patch(
        f"{base}/{kind}/{item['id']}", json={"mediator_id": mediator["id"]}, headers=headers
    )
    identity_url = f"{base}/identities/{item['identity']['id']}"

    # Not signed yet: no key anywhere, and no mailbox announced, whatever
    # mediator it picked — nothing could be encrypted to it.
    pending = (await client.get(identity_url, headers=headers)).json()["document"]
    assert "keyAgreement" not in pending and "service" not in pending
    assert vault.secrets == {}

    request = await sign(client, headers, tenant, item["identity"]["id"], wallet)
    [(path, jwk)] = vault.secrets.items()
    assert path == f"tenants/{tenant}/{kind}/{item['id']}/messaging"
    assert jwk["kty"] == "OKP" and jwk["crv"] == "X25519" and jwk["d"]
    key = multikey(_x(jwk))
    # The very entry the wallet signed lists it, and the mailbox with it.
    state = request["sign"]["document"]["state"]
    assert state["keyAgreement"] == [f"{state['id']}#{key}"]
    assert [s["type"] for s in state["service"]] == ["DIDCommMessaging"]
    detail = (await client.get(identity_url, headers=headers)).json()
    assert detail["signature"] == "signed"
    assert detail["signed_document"]["keyAgreement"] == [f"{detail['did']}#{key}"]

    # Signing again keeps the key it has.
    await client.patch(f"{base}/{kind}/{item['id']}", json={"mediator_id": None}, headers=headers)
    again = await sign(client, headers, tenant, item["identity"]["id"], wallet)
    assert again["sign"]["document"]["state"]["keyAgreement"] == state["keyAgreement"]
    assert "service" not in again["sign"]["document"]["state"]
    assert vault.secrets == {path: jwk}

    # Deleting it takes its keys out of the vault.
    gone = await client.delete(f"{base}/{kind}/{item['id']}", headers=headers)
    assert gone.status_code == 204
    assert vault.secrets == {}


async def test_mediators_and_the_tenant_get_no_key(
    client: AsyncClient, outbox: Outbox, dns: Dns, vault: MemoryVault
) -> None:
    headers, tenant = await _sign_in(client, outbox)
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    request = await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    assert "keyAgreement" not in request["sign"]["document"]["state"]
    assert vault.secrets == {}


async def test_no_signing_while_the_vault_is_down(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox)
    base = f"/api/v1/tenants/{tenant}"
    await ready(client, headers, tenant)
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    app.dependency_overrides[get_vault] = _DownVault
    response = await client.post(
        f"{base}/identities/{issuer['identity']['id']}/sign", json={}, headers=headers
    )
    assert response.status_code == 503 and response.json()["detail"] == "vault_unavailable"
    detail = await client.get(f"{base}/identities/{issuer['identity']['id']}", headers=headers)
    assert "keyAgreement" not in detail.json()["document"]
