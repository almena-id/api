"""Subscribing a tenant in tests, as the trust anchor's admin does."""

from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.test_directory import _sign_in

ANCHOR = "anchor@example.net"


async def subscribe(
    client: AsyncClient, outbox: Outbox, tenant_id: str, **subscription: Any
) -> dict[str, Any]:
    """The anchor's admin gives `tenant_id` a subscription (active, standard,
    open-ended unless told otherwise)."""
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    response = await client.put(
        f"/api/v1/tenants/{anchor}/accounts/{tenant_id}/subscription",
        json={"plan": "standard", "status": "active", **subscription},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def paid_sign_in(
    client: AsyncClient, outbox: Outbox, email: str
) -> tuple[dict[str, str], str]:
    """Headers for an account whose tenant is subscribed, and its id."""
    headers, tenant = await _sign_in(client, outbox, email)
    await subscribe(client, outbox, tenant)
    return headers, tenant
