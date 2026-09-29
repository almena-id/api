from typing import Any

from httpx import AsyncClient

from tests.conftest import Dns, Outbox
from tests.test_directory import _sign_in


async def _add(
    client: AsyncClient, headers: dict[str, str], base: str, typed: str
) -> dict[str, Any]:
    response = await client.post(base, json={"domain": typed}, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_domain_is_added_proved_and_named_in_the_did(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    assert (await client.get(base, headers=headers)).json() == []

    added = await _add(client, headers, base, "https://Acme.com/about")
    assert added["domain"] == "acme.com" and added["verified"] is False
    record = added["dns_record"]
    assert record["name"] == "_almena.acme.com" and record["value"].startswith("almena-verify=")
    again = await client.post(base, json={"domain": "acme.com"}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "domain_exists"
    bad = await client.post(base, json={"domain": "not a domain"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["detail"] == "domain_invalid"

    # Not proved yet: nothing in the DID document.
    missing = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert missing.status_code == 422 and missing.json()["detail"] == "dns_record_not_found"
    own = (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["identity"]
    url = f"/api/v1/tenants/{tenant}/identities/{own['id']}"
    assert "service" not in (await client.get(url, headers=headers)).json()["document"]

    dns.records[record["name"]] = [record["value"]]
    checked = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert checked.status_code == 200 and checked.json()["verified"] is True
    second = await _add(client, headers, base, "acme.org")
    dns.records[second["dns_record"]["name"]] = [second["dns_record"]["value"]]
    await client.post(f"{base}/{second['id']}/check", headers=headers)

    document = (await client.get(url, headers=headers)).json()["document"]
    assert "https://identity.foundation/.well-known/did-configuration/v1" in document["@context"]
    assert document["service"] == [
        {
            "id": f"{document['id']}#linked-domain",
            "type": "LinkedDomains",
            "serviceEndpoint": {"origins": ["https://acme.com", "https://acme.org"]},
        }
    ]

    removed = await client.delete(f"{base}/{second['id']}", headers=headers)
    assert removed.status_code == 204
    document = (await client.get(url, headers=headers)).json()["document"]
    assert document["service"][0]["serviceEndpoint"] == "https://acme.com"


async def test_only_admins_change_domains(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    added = await _add(client, ada, base, "acme.com")
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@acme.com", "role": "member"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@acme.com")
    # Members see them; only admins add, check or remove.
    assert [d["domain"] for d in (await client.get(base, headers=bob)).json()] == ["acme.com"]
    assert (await client.post(base, json={"domain": "x.com"}, headers=bob)).status_code == 403
    assert (await client.delete(f"{base}/{added['id']}", headers=bob)).status_code == 403
    # Nor anybody outside the tenant.
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(base, headers=eve)).status_code == 404
