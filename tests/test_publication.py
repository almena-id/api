import pytest
from httpx import AsyncClient

from tests.conftest import Outbox
from tests.test_directory import _sign_in


@pytest.mark.parametrize("kind", ["issuers", "verifiers", "mediators"])
async def test_a_draft_is_seen_only_inside_its_tenant(
    client: AsyncClient, outbox: Outbox, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/{kind}"
    body = {"name": "Uni", "description": "Degrees", "url": "https://mediator.example.org"}
    created = (await client.post(base, json=body, headers=headers)).json()
    assert created["published_at"] is None
    detail = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    slug = detail["did"].rsplit(":", 1)[1]

    # A draft: its DID does not resolve and the catalogue does not list it.
    identity_url = f"/api/v1/tenants/{tenant}/identities/{created['identity']['id']}"
    assert (await client.get(identity_url, headers=headers)).json()["published"] is False
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404
    assert (await client.get(f"/api/v1/catalog/{kind}")).json()["items"] == []

    published = await client.post(f"{base}/{created['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    moment = published.json()["published_at"]
    assert moment is not None
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 200
    assert (await client.get(identity_url, headers=headers)).json()["published"] is True
    items = (await client.get(f"/api/v1/catalog/{kind}")).json()["items"]
    assert [(i["did"], i["name"]) for i in items] == [(detail["did"], "Uni")]
    # The tenant by its DID, not by its name (which carries an email).
    assert items[0]["tenant"]["certified"] is False
    assert items[0]["tenant"]["legal_name"] is None
    assert items[0]["tenant"]["did"].startswith("did:web:")
    assert "ada@example.org" not in str(items)

    # Publishing again keeps the first moment.
    again = await client.post(f"{base}/{created['id']}/publish", headers=headers)
    # (SQLite drops the time zone on the way back; PostgreSQL keeps it.)
    assert again.json()["published_at"].rstrip("Z") == moment.rstrip("Z")

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


async def test_no_document_routes_through_a_draft_mediator(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{base}/mediators", json=body, headers=headers)).json()
    body = {"name": "Uni", "mediator_id": mediator["id"]}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    await client.post(f"{base}/issuers/{issuer['id']}/publish", headers=headers)
    slug = (await client.get(f"{base}/issuers/{issuer['id']}", headers=headers)).json()["did"]
    url = f"/ids/{slug.rsplit(':', 1)[1]}/did.json"

    assert "service" not in (await client.get(url)).json()
    await client.post(f"{base}/mediators/{mediator['id']}/publish", headers=headers)
    assert (await client.get(url)).json()["service"][0]["type"] == "DIDCommMessaging"


async def test_the_catalogue_pages(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/verifiers"
    for name in ("A", "B", "C"):
        item = (await client.post(base, json={"name": name}, headers=headers)).json()
        await client.post(f"{base}/{item['id']}/publish", headers=headers)
    first = (await client.get("/api/v1/catalog/verifiers?limit=2")).json()
    assert [i["name"] for i in first["items"]] == ["C", "B"]
    rest = (await client.get(f"/api/v1/catalog/verifiers?cursor={first['next_cursor']}")).json()
    assert [i["name"] for i in rest["items"]] == ["A"] and rest["next_cursor"] is None
    assert (await client.get("/api/v1/catalog/tenants")).status_code == 422


@pytest.mark.parametrize("kind", ["issuers", "verifiers", "mediators"])
async def test_an_admin_deletes_one_with_its_identity(
    client: AsyncClient, outbox: Outbox, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Uni", "url": "https://mediator.example.org"}
    item = (await client.post(f"{base}/{kind}", json=body, headers=headers)).json()
    await client.post(f"{base}/{kind}/{item['id']}/publish", headers=headers)
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
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{base}/mediators", json=body, headers=headers)).json()
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
