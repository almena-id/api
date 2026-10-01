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
