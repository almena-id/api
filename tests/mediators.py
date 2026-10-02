"""Helpers for tests that need mediators: a verified domain of the tenant's,
and a mediator listening on a subdomain of it."""

from typing import Any

from httpx import AsyncClient

from tests.conftest import Dns

Headers = dict[str, str]


async def verified_domain(
    client: AsyncClient, headers: Headers, tenant: str, dns: Dns, domain: str = "example.org"
) -> str:
    """The id of `domain`, linked to the tenant and proved; added when it is not."""
    base = f"/api/v1/tenants/{tenant}/domains"
    for item in (await client.get(base, headers=headers)).json():
        if item["domain"] == domain and item["verified"]:
            return str(item["id"])
    added = await client.post(base, json={"domain": domain}, headers=headers)
    assert added.status_code == 201, added.text
    record = added.json()["dns_record"]
    dns.records.setdefault(record["name"], []).append(record["value"])
    checked = await client.post(f"{base}/{added.json()['id']}/check", headers=headers)
    assert checked.status_code == 200, checked.text
    return str(added.json()["id"])


async def new_mediator(
    client: AsyncClient,
    headers: Headers,
    tenant: str,
    dns: Dns,
    *,
    name: str = "Relay",
    subdomain: str = "mediator",
    public: bool = False,
) -> dict[str, Any]:
    """A mediator at `https://{subdomain}.example.org`."""
    body = {
        "name": name,
        "subdomain": subdomain,
        "domain_id": await verified_domain(client, headers, tenant, dns),
        "public": public,
    }
    response = await client.post(f"/api/v1/tenants/{tenant}/mediators", json=body, headers=headers)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created
