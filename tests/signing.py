"""Helpers for tests that need identities signed: a wallet linked to an admin,
and the whole round of signing an identity's next log entry with it."""

from typing import Any

from httpx import AsyncClient

from tests.fake_wallet import FakeWallet

Headers = dict[str, str]


async def link_wallet(client: AsyncClient, headers: Headers, wallet: FakeWallet) -> None:
    asked = (
        await client.post("/api/v1/auth/wallet/requests", json={"purpose": "link"}, headers=headers)
    ).json()
    request = (await client.get(asked["request_uri"])).json()
    answered = await client.post(request["response_uri"], data=wallet.answer(request))
    assert answered.status_code == 204, answered.text
    result = await client.post(
        f"/api/v1/auth/wallet/requests/{asked['id']}/result",
        json={"poll": asked["poll"]},
        headers=headers,
    )
    assert result.json()["status"] == "linked", result.text


async def sign(
    client: AsyncClient,
    headers: Headers,
    tenant_id: str,
    identity_id: str,
    wallet: FakeWallet,
) -> dict[str, Any]:
    """Signs the identity's next entry; returns the request the wallet read."""
    asked = await client.post(
        f"/api/v1/tenants/{tenant_id}/identities/{identity_id}/sign", json={}, headers=headers
    )
    assert asked.status_code == 200, asked.text
    body = asked.json()
    request: dict[str, Any] = (await client.get(body["request_uri"])).json()
    answered = await client.post(request["response_uri"], json=wallet.answer(request))
    assert answered.status_code == 204, answered.text
    result = await client.post(
        f"/api/v1/auth/wallet/requests/{body['id']}/result",
        json={"poll": body["poll"]},
        headers=headers,
    )
    assert result.json()["status"] == "signed", result.text
    return request


async def ready(client: AsyncClient, headers: Headers, tenant_id: str) -> FakeWallet:
    """Links a new wallet to the admin and signs the tenant's own identity."""
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    own = (await client.get(f"/api/v1/tenants/{tenant_id}", headers=headers)).json()
    await sign(client, headers, tenant_id, own["identity"]["id"], wallet)
    return wallet


async def publish(
    client: AsyncClient,
    headers: Headers,
    tenant_id: str,
    kind: str,
    item_id: str,
    wallet: FakeWallet,
) -> dict[str, Any]:
    """Publishes an item by endorsing it; returns the request the wallet read."""
    asked = await client.post(
        f"/api/v1/tenants/{tenant_id}/{kind}/{item_id}/publish", json={}, headers=headers
    )
    assert asked.status_code == 200, asked.text
    body = asked.json()
    request: dict[str, Any] = (await client.get(body["request_uri"])).json()
    answered = await client.post(request["response_uri"], json=wallet.answer(request))
    assert answered.status_code == 204, answered.text
    result = await client.post(
        f"/api/v1/auth/wallet/requests/{body['id']}/result",
        json={"poll": body["poll"]},
        headers=headers,
    )
    assert result.json()["status"] == "signed", result.text
    return request
