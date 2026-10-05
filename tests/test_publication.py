from typing import Any

import pytest
from httpx import AsyncClient

from registry_api import webvh
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

    # Publishing is endorsing: the tenant signs its membership, and presents it.
    request = await publish(client, headers, tenant, kind, created["id"], wallet)
    assert request["sign"]["kind"] == "endorsement"
    endorsed = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    moment = endorsed["published_at"]
    assert moment is not None and endorsed["endorsed_until"] is not None
    assert endorsed["whois_url"].endswith(f"/ids/{slug}/whois.vp")
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 200
    whois = await client.get(f"/ids/{slug}/whois.vp")
    assert whois.status_code == 200 and whois.headers["content-type"] == "application/vp"
    _check_endorsement(whois.json(), detail["did"], detail["document"]["controller"], wallet)
    assert (await client.get(identity_url, headers=headers)).json()["published"] is True
    items = (await client.get(f"/api/v1/catalog/{kind}")).json()["items"]
    assert [(i["did"], i["name"]) for i in items] == [(detail["did"], "Uni")]
    # The tenant by its DID, not by its name (which carries an email).
    assert items[0]["tenant"]["did"].startswith("did:webvh:")
    assert "ada@example.org" not in str(items)

    # Publishing again renews the endorsement and keeps the first moment.
    await publish(client, headers, tenant, kind, created["id"], wallet)
    again = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    # (SQLite drops the time zone on the way back; PostgreSQL keeps it.)
    assert again["published_at"].rstrip("Z") == moment.rstrip("Z")

    back = await client.post(f"{base}/{created['id']}/unpublish", headers=headers)
    assert back.json()["published_at"] is None
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404
    assert (await client.get(f"/ids/{slug}/whois.vp")).status_code == 404
    assert (await client.get(f"/api/v1/catalog/{kind}")).json()["items"] == []


async def test_only_the_flows_signers_publish(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=ada)).json()
    invite = {"email": "bob@example.org", "role": "member"}
    await client.post(f"{base}/invitations", json=invite, headers=ada)
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    denied = await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=bob)
    assert denied.status_code == 403 and denied.json()["detail"] == "not_a_signer"

    # Nor in somebody else's tenant.
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (
        await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=eve)
    ).status_code == 404


async def test_no_document_routes_through_a_draft_mediator(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    body = {"name": "Uni", "mediator_id": mediator["id"]}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    await publish(client, headers, tenant, "issuers", issuer["id"], wallet)
    slug = (await client.get(f"{base}/issuers/{issuer['id']}", headers=headers)).json()["did"]
    url = f"/ids/{slug.rsplit(':', 1)[1]}/did.json"

    assert "service" not in (await client.get(url)).json()
    # Publishing the mediator changes what the issuer should say: signed, it says it.
    await publish(client, headers, tenant, "mediators", mediator["id"], wallet)
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    assert (await client.get(url)).json()["service"][0]["type"] == "DIDCommMessaging"


async def test_the_catalogue_pages(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/verifiers"
    wallet = await ready(client, headers, tenant)
    for name in ("A", "B", "C"):
        item = (await client.post(base, json={"name": name}, headers=headers)).json()
        await sign(client, headers, tenant, item["identity"]["id"], wallet)
        await publish(client, headers, tenant, "verifiers", item["id"], wallet)
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
    await publish(client, headers, tenant, kind, item["id"], wallet)
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


def _check_endorsement(vp: dict[str, Any], did: str, tenant_did: str, wallet: FakeWallet) -> None:
    """What a verifier checks in a component's whois.vp: presented for it by its
    tenant, and the tenant's credential inside saying it belongs to it."""
    key = wallet.did.removeprefix("did:key:")
    assert vp["holder"] == did
    unsigned = {k: v for k, v in vp.items() if k != "proof"}
    webvh.check_proof(unsigned, vp["proof"], [key], controller=tenant_did, purpose="authentication")
    vc = vp["verifiableCredential"][0]
    assert vc["type"] == ["VerifiableCredential", "AlmenaMembership"]
    assert vc["issuer"] == tenant_did
    assert vc["credentialSubject"]["id"] == did
    assert vc["credentialSubject"]["memberOf"]["id"] == tenant_did
    webvh.check_proof(
        {k: v for k, v in vc.items() if k != "proof"}, vc["proof"], [key], controller=tenant_did
    )


async def test_a_key_the_items_document_does_not_name_cannot_endorse_it(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    # A new admin key after the issuer's document was signed: not in it yet.
    other = FakeWallet()
    await link_wallet(client, headers, other)
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    await sign(client, headers, tenant, own, wallet)
    asked = await client.post(f"{base}/issuers/{issuer['id']}/publish", json={}, headers=headers)
    assert asked.status_code == 200  # the first key still signs for both
    asked_by_new = await client.post(
        f"{base}/issuers/{issuer['id']}/publish", json={}, headers=headers
    )
    request = (await client.get(asked_by_new.json()["request_uri"])).json()
    refused = await client.post(request["response_uri"], json=other.answer(request))
    assert refused.status_code == 400 and refused.json()["detail"] == "not_a_signer"
