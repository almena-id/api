import json
import uuid
from typing import cast

import pytest
from httpx import AsyncClient

from registry_api.mediators import did_web
from tests.conftest import Dns, Outbox
from tests.mediators import new_mediator, verified_domain
from tests.signing import publish, ready, sign
from tests.test_directory import _sign_in


async def test_a_mediator_gets_an_identity_that_publishes_its_address(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    mediator = await new_mediator(client, headers, tenant, dns, subdomain=" EU.Mediator. ")
    # A subdomain of the tenant's domain, lowercased.
    assert mediator["url"] == "https://eu.mediator.example.org"
    identity = mediator["identity"]
    assert isinstance(identity, dict) and identity["name"] == "Relay"

    listed = (await client.get(f"{base}/mediators", headers=headers)).json()
    assert [m["id"] for m in listed["items"]] == [mediator["id"]]

    detail = (await client.get(f"{base}/identities/{identity['id']}", headers=headers)).json()
    assert detail["used_by"] == [{"kind": "mediator", "id": mediator["id"], "name": "Relay"}]
    assert detail["document"]["service"][0]["serviceEndpoint"] == {
        "uri": "https://eu.mediator.example.org",
        "accept": ["didcomm/v2"],
    }
    opened = (await client.get(f"{base}/mediators/{mediator['id']}", headers=headers)).json()
    assert opened["did"] == detail["did"]


@pytest.mark.parametrize("typed", [" ", ".", "-relay", "relay-", "re_lay", "a..b", "x" * 64])
async def test_the_subdomain_must_be_a_valid_one(
    client: AsyncClient, outbox: Outbox, dns: Dns, typed: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = {
        "name": "Relay",
        "subdomain": typed,
        "domain_id": await verified_domain(client, headers, tenant, dns),
    }
    response = await client.post(f"/api/v1/tenants/{tenant}/mediators", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "subdomain_invalid"


async def test_the_domain_must_be_the_tenants_and_verified(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    ada, adas = await _sign_in(client, outbox, "ada@example.org")
    bob, bobs = await _sign_in(client, outbox, "bob@example.org")
    url = f"/api/v1/tenants/{adas}/mediators"

    async def refused(domain_id: str) -> str:
        body = {"name": "Relay", "subdomain": "relay", "domain_id": domain_id}
        response = await client.post(url, json=body, headers=ada)
        assert response.status_code == 422
        detail: str = response.json()["detail"]
        return detail

    assert await refused(str(uuid.uuid4())) == "domain_not_found"
    assert await refused(await verified_domain(client, bob, bobs, dns)) == "domain_not_found"
    added = await client.post(
        f"/api/v1/tenants/{adas}/domains", json={"domain": "acme.com"}, headers=ada
    )
    assert await refused(added.json()["id"]) == "domain_unverified"


async def test_a_moved_address_follows_the_rules_of_a_new_one(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    ada, adas = await _sign_in(client, outbox, "ada@example.org")
    bob, bobs = await _sign_in(client, outbox, "bob@example.org")
    mediator = await new_mediator(client, ada, adas, dns)
    url = f"/api/v1/tenants/{adas}/mediators/{mediator['id']}"
    domain_id = await verified_domain(client, ada, adas, dns)
    unverified = await client.post(
        f"/api/v1/tenants/{adas}/domains", json={"domain": "acme.com"}, headers=ada
    )

    async def refused(body: dict[str, object]) -> str:
        response = await client.patch(url, json=body, headers=ada)
        assert response.status_code == 422, response.text
        detail: str = response.json()["detail"]
        return detail

    # A whole address is not taken: it moves as it is made.
    plain = await client.patch(url, json={"url": "http://localhost:8080"}, headers=ada)
    assert plain.status_code == 200 and plain.json()["url"] == mediator["url"]
    assert await refused({"subdomain": "relay"}) == "domain_not_found"
    assert await refused({"domain_id": domain_id}) == "subdomain_invalid"
    assert await refused({"subdomain": "re_lay", "domain_id": domain_id}) == "subdomain_invalid"
    assert (
        await refused(
            {"subdomain": "relay", "domain_id": await verified_domain(client, bob, bobs, dns)}
        )
        == "domain_not_found"
    )
    assert (
        await refused({"subdomain": "relay", "domain_id": unverified.json()["id"]})
        == "domain_unverified"
    )
    kept = (await client.get(url, headers=ada)).json()
    assert kept["url"] == mediator["url"]


async def test_renaming_and_moving_keep_the_did(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    mediator = await new_mediator(client, headers, tenant, dns)
    url = f"/api/v1/tenants/{tenant}/mediators/{mediator['id']}"
    before = (await client.get(url, headers=headers)).json()

    body = {
        "name": "Main relay",
        "subdomain": "Relay",
        "domain_id": await verified_domain(client, headers, tenant, dns),
    }
    changed = await client.patch(url, json=body, headers=headers)
    assert changed.status_code == 200, changed.text
    after = changed.json()
    assert after["did"] == before["did"]
    assert after["url"] == "https://relay.example.org"
    # Its identity is renamed with it.
    assert after["identity"]["name"] == "Main relay"

    blank = await client.patch(url, json={"name": " "}, headers=headers)
    assert blank.status_code == 422 and blank.json()["detail"] == "name_required"


async def test_issuers_route_through_their_mediator(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    identity = cast(dict[str, str], mediator["identity"])
    await sign(client, headers, tenant, identity["id"], wallet)
    did = (await client.get(f"{base}/mediators/{mediator['id']}", headers=headers)).json()["did"]

    body = {"name": "Uni", "mediator_id": mediator["id"]}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    assert issuer["mediator"] == {"id": mediator["id"], "name": "Relay", "own": True}
    # Both signed and published: a draft's DID does not resolve.
    await publish(client, headers, tenant, "mediators", str(mediator["id"]), wallet)
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    await publish(client, headers, tenant, "issuers", issuer["id"], wallet)

    slug = (
        (await client.get(f"{base}/identities/{issuer['identity']['id']}", headers=headers))
        .json()["did"]
        .rsplit(":", 1)[1]
    )
    log = (await client.get(f"/ids/{slug}/did.jsonl")).text.splitlines()
    published = json.loads(log[-1])["state"]
    # Routed through the did:web the mediator answers as, whose document has
    # its keys; the registry's DID for it names only where it listens.
    assert published["service"][0]["serviceEndpoint"]["uri"] == "did:web:mediator.example.org"
    assert did != "did:web:mediator.example.org"

    # Without one, nothing to route through.
    plain = (await client.post(f"{base}/verifiers", json={"name": "Desk"}, headers=headers)).json()
    assert plain["mediator"] is None


async def test_mediators_stay_in_their_tenant(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, bob_tenant = await _sign_in(client, outbox, "bob@example.org")
    mediator = await new_mediator(client, ada, ada_tenant, dns)

    body = {"name": "Bob's", "mediator_id": mediator["id"]}
    response = await client.post(f"/api/v1/tenants/{bob_tenant}/issuers", json=body, headers=bob)
    assert response.status_code == 422
    assert response.json()["detail"] == "mediator_not_found"

    other = f"/api/v1/tenants/{bob_tenant}/mediators/{mediator['id']}"
    assert (await client.get(other, headers=bob)).status_code == 404
    own = f"/api/v1/tenants/{ada_tenant}/mediators/{mediator['id']}"
    assert (await client.get(own, headers=bob)).status_code == 404


def test_a_mediator_answers_as_the_did_web_of_its_address() -> None:
    assert did_web("https://mediator.almena.id") == "did:web:mediator.almena.id"
    assert did_web("http://localhost:8080") == "did:web:localhost%3A8080"
    assert did_web("https://example.org/relay/eu") == "did:web:example.org:relay:eu"
