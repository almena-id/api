"""The public offers one by one and by issuer, and the credential types an
issuer grants with the forms that make them offers."""

import uuid

from httpx import AsyncClient

from registry_api import answers
from tests.conftest import Dns, Outbox
from tests.test_applications import _offer
from tests.test_directory import _sign_in


async def _club_slug(client: AsyncClient, did: str) -> str:
    items = (await client.get("/api/v1/catalog/issuers")).json()["items"]
    return str(next(item["slug"] for item in items if item["did"] == did))


async def test_an_offer_shows_its_form_and_how_large_a_file_may_be(
    client: AsyncClient, outbox: Outbox
) -> None:
    _, club, _, _ = await _offer(client, outbox)
    slug = await _club_slug(client, club.did)
    offer = (await client.get(f"/api/v1/catalog/issuers/{slug}/offers/membership")).json()
    assert offer["issuer"]["slug"] == slug and offer["category"]["id"]
    fields = {f["key"]: f for f in offer["form"]["fields"]}
    # A file field says the most a holder may upload; no other field does.
    assert fields["id_scan"]["max_bytes"] == answers.MAX_FILE == 10 * 1024 * 1024
    assert all("max_bytes" not in f for key, f in fields.items() if key != "id_scan")
    asked = offer["form"]["credentials"][0]
    assert asked["type"] == "verified_email" and asked["labels"]["en"]

    together = (await client.get(f"/api/v1/catalog/issuers/{slug}/offers")).json()
    assert together["issuer"]["did"] == club.did and together["published_at"]
    assert [o["id"] for o in together["offers"]] == ["membership"]


async def test_what_is_not_offered_is_not_found(client: AsyncClient, outbox: Outbox) -> None:
    _, club, headers, tenant = await _offer(client, outbox)
    slug = await _club_slug(client, club.did)
    for path, code in (
        (f"/api/v1/catalog/issuers/{slug}/offers/employment", "offer_not_found"),
        ("/api/v1/catalog/issuers/iss_nothere/offers/membership", "offer_not_found"),
        ("/api/v1/catalog/issuers/iss_nothere/offers", "issuer_not_found"),
    ):
        response = await client.get(path)
        assert response.status_code == 404 and response.json()["detail"] == code, path

    # Unpublished, its offers are gone from everywhere public.
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    issuer_id = issuers["items"][0]["id"]
    taken = await client.post(
        f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/unpublish", headers=headers
    )
    assert taken.status_code == 200, taken.text
    one = await client.get(f"/api/v1/catalog/issuers/{slug}/offers/membership")
    assert one.status_code == 404 and one.json()["detail"] == "offer_not_found"
    assert (await client.get(f"/api/v1/catalog/issuers/{slug}/offers")).status_code == 404
    listed = (await client.get("/api/v1/catalog/offers")).json()
    assert all(o["issuer"]["slug"] != slug for o in listed["items"])
    started = await client.post("/api/v1/applications", json={"issuer": slug, "type": "membership"})
    assert started.status_code == 404 and started.json()["detail"] == "offer_not_found"


async def test_an_issuer_offers_only_with_its_own_tenants_forms(
    client: AsyncClient, outbox: Outbox
) -> None:
    _, _, headers, tenant = await _offer(client, outbox)
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    url = f"/api/v1/tenants/{tenant}/issuers/{issuers['items'][0]['id']}/credential-types"
    before = (await client.get(url, headers=headers)).json()
    assert before["types"] == ["membership"] and set(before["forms"]) == {"membership"}

    eve, eves = await _sign_in(client, outbox, "eve@example.org")
    theirs = (
        await client.post(
            f"/api/v1/tenants/{eves}/forms",
            json={"name": {"en": "Eve's"}, "fields": [{"ref": "given_name"}]},
            headers=eve,
        )
    ).json()
    refused = await client.put(
        url, json={"types": ["membership"], "forms": {"membership": theirs["id"]}}, headers=headers
    )
    assert refused.status_code == 422 and refused.json()["detail"] == "request_form_invalid"
    # Nor does she reach the issuer, from her tenant or Ada's.
    assert (await client.get(url, headers=eve)).status_code == 404
    elsewhere = f"/api/v1/tenants/{eves}/issuers/{issuers['items'][0]['id']}/credential-types"
    put = await client.put(elsewhere, json={"types": []}, headers=eve)
    assert put.status_code == 404 and put.json()["detail"] == "issuer_not_found"
    unknown = f"/api/v1/tenants/{tenant}/issuers/{uuid.uuid4()}/credential-types"
    assert (await client.get(unknown, headers=headers)).json()["detail"] == "issuer_not_found"
    # Unchanged by what was refused.
    assert (await client.get(url, headers=headers)).json() == before

    # Granted without a form, a type is not offered.
    granted = await client.put(url, json={"types": ["membership"]}, headers=headers)
    assert granted.json() == {"types": ["membership"], "forms": {}}
    listed = (await client.get("/api/v1/catalog/offers")).json()
    assert all(o["credential_type"]["id"] != "membership" for o in listed["items"])


async def test_an_issuer_shows_only_the_domains_its_tenant_has_proved(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    _, club, headers, tenant = await _offer(client, outbox)
    slug = await _club_slug(client, club.did)
    one = f"/api/v1/catalog/issuers/{slug}/offers/membership"
    together = f"/api/v1/catalog/issuers/{slug}/offers"

    def listed(body: dict[str, object]) -> list[object]:
        items = body["items"]
        assert isinstance(items, list)
        return [o["issuer"]["domains"] for o in items if o["issuer"]["slug"] == slug]

    # None proved yet: an empty list everywhere.
    assert (await client.get(one)).json()["issuer"]["domains"] == []
    assert (await client.get(together)).json()["issuer"]["domains"] == []
    assert listed((await client.get("/api/v1/catalog/offers")).json()) == [[]]

    base = f"/api/v1/tenants/{tenant}/domains"
    proved, pending = [
        (await client.post(base, json={"domain": name}, headers=headers)).json()
        for name in ("club.example", "club.test")
    ]
    record = proved["dns_record"]
    dns.records[record["name"]] = [record["value"]]
    checked = await client.post(f"{base}/{proved['id']}/check", headers=headers)
    assert checked.status_code == 200 and checked.json()["verified"] is True
    assert pending["verified"] is False

    # Only the verified one, on the offer, the issuer's page and the listing.
    assert (await client.get(one)).json()["issuer"]["domains"] == ["club.example"]
    assert (await client.get(together)).json()["issuer"]["domains"] == ["club.example"]
    assert listed((await client.get("/api/v1/catalog/offers")).json()) == [["club.example"]]

    # Another tenant's issuer does not show them.
    mail = (await client.get("/api/v1/catalog/offers")).json()["items"]
    assert all(o["issuer"]["domains"] == [] for o in mail if o["issuer"]["slug"] != slug)

    # Unpublished, the issuer and its domains are gone.
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    issuer_id = issuers["items"][0]["id"]
    await client.post(f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/unpublish", headers=headers)
    assert (await client.get(together)).status_code == 404
    assert (await client.get(one)).status_code == 404
