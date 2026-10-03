from typing import Any

from httpx import AsyncClient

from registry_api import credential_catalog as credentials
from registry_api import field_catalog as fields
from tests.conftest import Outbox
from tests.signing import publish, ready, sign
from tests.test_directory import _sign_in


async def test_the_credential_catalogue_is_published(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/catalog/credentials")).json()
    assert body == (await client.get("/schemas/credentials/v1")).json()
    assert body["version"] == "v1"
    types = {item["id"]: item for item in body["types"]}
    assert {"pid", "membership", "academic_degree", "employment"} <= set(types)

    membership = types["membership"]
    assert membership["issuance"] == "almena"
    assert membership["formats"] == {
        "dc+sd-jwt": {"vct": "https://almena.id/credentials/membership/v1"},
        "jwt_vc_json": {"type": ["VerifiableCredential", "MembershipCredential"]},
    }
    assert membership["schema"] == "https://almena.id/schemas/credentials/v1/membership.json"
    assert membership["metadata"] == ("https://almena.id/.well-known/vct/credentials/membership/v1")
    assert {"field": "member_number", "required": True} in membership["claims"]

    # The EU PID is asked for, not issued by tenants; its metadata is not ours.
    pid = types["pid"]
    assert pid["issuance"] == "external" and "metadata" not in pid
    assert pid["formats"]["dc+sd-jwt"] == {"vct": "urn:eudi:pid:1"}
    assert pid["formats"]["mso_mdoc"] == {"doctype": "eu.europa.ec.eudi.pid.1"}
    assert (await client.get("/schemas/credentials/v2")).status_code == 404


async def test_each_type_publishes_its_schema_and_type_metadata(client: AsyncClient) -> None:
    response = await client.get("/schemas/credentials/v1/academic_degree.json")
    assert response.headers["content-type"].startswith("application/schema+json")
    schema = response.json()
    assert schema["$id"] == "https://almena.id/schemas/credentials/v1/academic_degree.json"
    assert schema["properties"]["education_level"] == {
        "$ref": "https://almena.id/schemas/fields/v1/education_level.json"
    }
    assert "birthdate" not in schema["required"] and "degree_name" in schema["required"]

    metadata = (await client.get("/.well-known/vct/credentials/membership/v1")).json()
    assert metadata["vct"] == "https://almena.id/credentials/membership/v1"
    assert metadata["schema_uri"] == "https://almena.id/schemas/credentials/v1/membership.json"
    assert {
        "locale": "es",
        "name": "Afiliación",
        "description": metadata["display"][1]["description"],
    } == metadata["display"][1]
    member = next(c for c in metadata["claims"] if c["path"] == ["member_number"])
    assert member["mandatory"] is True
    assert {"locale": "es", "label": "Número de socio"} in member["display"]

    residence = (await client.get("/.well-known/vct/credentials/residence/v1")).json()
    assert ["address", "postal_code"] in [claim["path"] for claim in residence["claims"]]
    for missing in (
        "/.well-known/vct/credentials/pid/v1",
        "/.well-known/vct/credentials/membership/v2",
        "/schemas/credentials/v1/nope.json",
    ):
        assert (await client.get(missing)).status_code == 404


def test_every_claim_is_a_catalogue_field_with_every_label() -> None:
    for item in credentials.TYPES:
        assert item.category in credentials.CATEGORIES
        assert set(item.labels) == set(item.descriptions) == set(fields.LANGUAGES)
        for claim in item.claims:
            assert claim.field in fields.BY_ID, (item.id, claim.field)


async def test_an_issuer_declares_what_it_grants(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    issuers = f"/api/v1/tenants/{tenant}/issuers"
    created = (
        await client.post(issuers, json={"name": "Club", "description": "Members"}, headers=headers)
    ).json()
    url = f"{issuers}/{created['id']}/credential-types"
    assert (await client.get(url, headers=headers)).json() == {"types": [], "forms": {}}

    saved = await client.put(url, json={"types": ["membership", "employment"]}, headers=headers)
    # In the catalogue's order.
    assert saved.status_code == 200 and saved.json() == {
        "types": ["employment", "membership"],
        "forms": {},
    }
    for bad in (["pid"], ["nope"]):
        refused = await client.put(url, json={"types": bad}, headers=headers)
        assert refused.status_code == 422
        assert refused.json()["detail"] == "credential_type_invalid"

    # The public catalogue says what each issuer grants.
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, created["identity"]["id"], wallet)
    await publish(client, headers, tenant, "issuers", created["id"], wallet)
    items = (await client.get("/api/v1/catalog/issuers")).json()["items"]
    assert items[0]["credential_types"] == ["employment", "membership"]

    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(url, headers=eve)).status_code == 404


async def test_the_public_catalogue_lists_every_offer_page_by_page(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    issuers = f"/api/v1/tenants/{tenant}/issuers"
    form = (
        await client.post(
            f"/api/v1/tenants/{tenant}/forms",
            json={"name": {"en": "Apply"}, "fields": [{"ref": "given_name"}]},
            headers=headers,
        )
    ).json()
    wallet = await ready(client, headers, tenant)

    async def issuer(name: str, forms: list[str], published: bool = True) -> dict[str, Any]:
        created: dict[str, Any] = (
            await client.post(issuers, json={"name": name}, headers=headers)
        ).json()
        saved = await client.put(
            f"{issuers}/{created['id']}/credential-types",
            json={
                "types": ["employment", "membership"],
                "forms": {t: form["id"] for t in forms},
            },
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        await sign(client, headers, tenant, created["identity"]["id"], wallet)
        if published:
            await publish(client, headers, tenant, "issuers", created["id"], wallet)
        return created

    # Granted without a form, or not published: not offered.
    await issuer("Old club", ["employment", "membership"], published=False)
    await issuer("Union", ["employment", "membership"])
    await issuer("Club", ["membership"])

    seen: list[tuple[str, str]] = []
    cursor: str | None = None
    while True:
        query = "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = await client.get(f"/api/v1/catalog/offers{query}")
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["total"] == 3
        seen += [(o["issuer"]["name"], o["credential_type"]["id"]) for o in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    # Newest issuer first; each issuer's types in the order it grants them.
    assert seen == [("Club", "membership"), ("Union", "employment"), ("Union", "membership")]

    first = (await client.get("/api/v1/catalog/offers")).json()["items"][0]
    assert first["issuer"]["slug"].startswith("iss")
    assert first["issuer"]["did"]
    assert first["credential_type"]["labels"]["en"]
    assert first["credential_type"]["category"] == {
        "id": "membership",
        "labels": {"en": "Membership", "es": "Afiliación"},
    }
    bad = await client.get("/api/v1/catalog/offers?cursor=nope")
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid_cursor"


async def test_the_catalogue_finds_issuers_by_what_they_grant_and_their_name(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    issuers = f"/api/v1/tenants/{tenant}/issuers"
    wallet = await ready(client, headers, tenant)

    async def issuer(name: str, types: list[str]) -> dict[str, Any]:
        created: dict[str, Any] = (
            await client.post(issuers, json={"name": name}, headers=headers)
        ).json()
        await client.put(
            f"{issuers}/{created['id']}/credential-types", json={"types": types}, headers=headers
        )
        await sign(client, headers, tenant, created["identity"]["id"], wallet)
        await publish(client, headers, tenant, "issuers", created["id"], wallet)
        return created

    await issuer("North Gym", ["membership"])
    await issuer("Union of Teachers", ["membership", "employment"])
    await issuer("City Hall", ["residence"])
    await issuer("South Gym", ["membership"])

    async def names(query: str) -> list[str]:
        found: list[str] = []
        cursor = ""
        while True:
            page = await client.get(f"/api/v1/catalog/issuers?limit=1{query}{cursor}")
            assert page.status_code == 200, page.text
            found += [item["name"] for item in page.json()["items"]]
            if not page.json()["next_cursor"]:
                return found
            cursor = f"&cursor={page.json()['next_cursor']}"

    # Newest first, a page of one at a time, only those granting the type.
    assert await names("&grants=membership") == ["South Gym", "Union of Teachers", "North Gym"]
    assert await names("&grants=membership&q=gYm") == ["South Gym", "North Gym"]
    assert await names("&q=hall") == ["City Hall"]
    assert await names("&q=%25") == []
    assert await names("&grants=enrollment") == []
    hall = (await client.get("/api/v1/catalog/issuers?q=hall")).json()["items"][0]
    assert await names(f"&q={hall['did'][-12:]}") == ["City Hall"]
    refused = await client.get("/api/v1/catalog/verifiers?grants=membership")
    assert refused.status_code == 422 and refused.json()["detail"] == "grants_issuers_only"
