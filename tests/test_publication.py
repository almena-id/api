from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import new_mediator, verified_domain
from tests.signing import link_wallet, publish, ready, sign
from tests.test_directory import _sign_in


@pytest.mark.parametrize("kind", ["issuers", "verifiers", "mediators"])
async def test_a_draft_is_seen_only_inside_its_tenant(
    client: AsyncClient, outbox: Outbox, dns: Dns, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/{kind}"
    body: dict[str, Any] = {"name": "Uni", "description": {"en": "Degrees"}}
    if kind == "mediators":
        body["subdomain"] = "mediator"
        body["domain_id"] = await verified_domain(client, headers, tenant, dns)
    created = (await client.post(base, json=body, headers=headers)).json()
    assert created["published_at"] is None
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, created["identity"]["id"], wallet)
    detail = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    slug = detail["did"].rsplit(":", 1)[1]

    # A draft: its DID does not resolve and the catalogue does not list it.
    identity_url = f"/api/v1/tenants/{tenant}/identities/{created['identity']['id']}"
    assert (await client.get(identity_url, headers=headers)).json()["published"] is False
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404
    assert (await client.get(f"/api/v1/catalog/{kind}")).json()["items"] == []

    # Publishing is an admin's decision, not a signature: nothing asks the wallet.
    published = await publish(client, headers, tenant, kind, created["id"])
    moment = published["published_at"]
    assert moment is not None and published["did"] == detail["did"]
    assert "whois_url" not in published and "endorsed_until" not in published
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 200
    assert (await client.get(f"/ids/{slug}/whois.vp")).status_code == 404
    assert (await client.get(identity_url, headers=headers)).json()["published"] is True
    items = (await client.get(f"/api/v1/catalog/{kind}")).json()["items"]
    assert [(i["did"], i["name"]) for i in items] == [(detail["did"], "Uni")]
    # The tenant by its DID, not by its name (which carries an email).
    assert items[0]["tenant"]["did"].startswith("did:webvh:")
    assert "ada@example.org" not in str(items)

    # Publishing again keeps the first moment.
    await publish(client, headers, tenant, kind, created["id"])
    again = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    # (SQLite drops the time zone on the way back; PostgreSQL keeps it.)
    assert again["published_at"].rstrip("Z") == moment.rstrip("Z")

    back = await client.post(f"{base}/{created['id']}/unpublish", headers=headers)
    assert back.json()["published_at"] is None
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404
    assert (await client.get(f"/api/v1/catalog/{kind}")).json()["items"] == []


async def test_only_admins_publish(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    invite = {"email": "bob@example.org", "role": "member"}
    await client.post(f"{base}/invitations", json=invite, headers=ada)
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    denied = await client.post(f"{base}/issuers/{issuer['id']}/publish", headers=bob)
    assert denied.status_code == 403 and denied.json()["detail"] == "not_admin"

    # Nor in somebody else's tenant.
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (
        await client.post(f"{base}/issuers/{issuer['id']}/publish", headers=eve)
    ).status_code == 404


async def test_nothing_is_published_under_an_unsigned_did(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    url = f"{base}/issuers/{issuer['id']}/publish"
    pending = await client.post(url, headers=headers)
    assert pending.status_code == 409 and pending.json()["detail"] == "identity_pending"

    # Its own DID signed before the tenant's: the tenant's is still missing.
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    tenant_pending = await client.post(url, headers=headers)
    assert tenant_pending.status_code == 409
    assert tenant_pending.json()["detail"] == "tenant_pending"


async def test_no_document_routes_through_a_draft_mediator(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    await client.patch(base, json={"mediator_id": mediator["id"]}, headers=headers)
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    await sign(client, headers, tenant, own, wallet)
    did = (await client.get(f"{base}/identities/{own}", headers=headers)).json()["did"]
    url = f"/ids/{did.rsplit(':', 1)[1]}/did.json"

    services = [s["type"] for s in (await client.get(url)).json().get("service", [])]
    assert "DIDCommMessaging" not in services
    # Publishing the mediator changes what the tenant should say: signed, it says it.
    await publish(client, headers, tenant, "mediators", mediator["id"])
    await sign(client, headers, tenant, own, wallet)
    services = [s["type"] for s in (await client.get(url)).json()["service"]]
    assert "DIDCommMessaging" in services


async def test_the_catalogue_pages(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/verifiers"
    wallet = await ready(client, headers, tenant)
    for name in ("A", "B", "C"):
        item = (await client.post(base, json={"name": name}, headers=headers)).json()
        await sign(client, headers, tenant, item["identity"]["id"], wallet)
        await publish(client, headers, tenant, "verifiers", item["id"])
    first = (await client.get("/api/v1/catalog/verifiers?limit=2")).json()
    assert [i["name"] for i in first["items"]] == ["C", "B"]
    rest = (await client.get(f"/api/v1/catalog/verifiers?cursor={first['next_cursor']}")).json()
    assert [i["name"] for i in rest["items"]] == ["A"] and rest["next_cursor"] is None
    assert (await client.get("/api/v1/catalog/tenants")).status_code == 422


@pytest.mark.parametrize("kind", ["issuers", "verifiers", "mediators"])
async def test_an_admin_deletes_one_with_its_identity(
    client: AsyncClient, outbox: Outbox, dns: Dns, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Uni"}
    if kind == "mediators":
        body["subdomain"] = "mediator"
        body["domain_id"] = await verified_domain(client, headers, tenant, dns)
    item = (await client.post(f"{base}/{kind}", json=body, headers=headers)).json()
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, item["identity"]["id"], wallet)
    await publish(client, headers, tenant, kind, item["id"])
    detail = (await client.get(f"{base}/{kind}/{item['id']}", headers=headers)).json()
    assert detail["document"]["id"] == detail["did"]
    assert detail["document_url"].endswith("/did.json")

    deleted = await client.delete(f"{base}/{kind}/{item['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"{base}/{kind}/{item['id']}", headers=headers)).status_code == 404
    identity = item["identity"]["id"]
    assert (await client.get(f"{base}/identities/{identity}", headers=headers)).status_code == 404
    slug = detail["did"].rsplit(":", 1)[1]
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404
    assert (await client.get(f"/api/v1/catalog/{kind}")).json()["items"] == []


async def test_deleting_a_mediator_leaves_its_users_without_one(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    mediator = await new_mediator(client, headers, tenant, dns)
    body = {"name": "Uni", "mediator_id": mediator["id"]}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    await client.patch(base, json={"mediator_id": mediator["id"]}, headers=headers)

    await client.delete(f"{base}/mediators/{mediator['id']}", headers=headers)
    assert (await client.get(f"{base}/issuers/{issuer['id']}", headers=headers)).json()[
        "mediator"
    ] is None
    assert (await client.get(base, headers=headers)).json()["mediator"] is None


async def test_only_admins_delete(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    invite = {"email": "bob@example.org", "role": "member"}
    await client.post(f"{base}/invitations", json=invite, headers=ada)
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    denied = await client.delete(f"{base}/issuers/{issuer['id']}", headers=bob)
    assert denied.status_code == 403
    assert (await client.get(f"{base}/issuers/{issuer['id']}", headers=ada)).status_code == 200
