from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import field_catalog as catalog
from registry_api import trust_anchor


async def test_the_catalogue_is_published(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/catalog/fields")).json()
    assert body == (await client.get("/schemas/fields/v1")).json()
    assert body["version"] == "v1" and body["languages"] == ["en", "es"]
    fields = {item["id"]: item for item in body["fields"]}
    assert {"given_name", "birthdate", "address", "identity_document", "document_file"} <= set(
        fields
    )
    assert fields["given_name"]["labels"] == {"en": "Given name", "es": "Nombre"}
    assert fields["given_name"]["schema"] == "https://almena.id/schemas/fields/v1/given_name.json"
    assert fields["document_file"]["repeatable"] is True
    assert [part["key"] for part in fields["address"]["parts"]][:2] == [
        "street_address",
        "house_number",
    ]
    countries = {code["value"]: code["labels"] for code in body["domains"]["country"]["codes"]}
    assert len(countries) == 249 and countries["ES"] == {"en": "Spain", "es": "España"}
    assert body["domains"]["sex"]["source"] == "ISO/IEC 5218"
    formats = body["domains"]["file_format"]["codes"]
    assert formats[0] == {
        "value": "pdf",
        "labels": {"en": "PDF", "es": "PDF"},
        "media_type": "application/pdf",
    }
    assert (await client.get("/schemas/fields/v2")).status_code == 404


async def test_each_field_publishes_its_json_schema(client: AsyncClient) -> None:
    response = await client.get("/schemas/fields/v1/address.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/schema+json")
    schema = response.json()
    assert schema["$id"] == "https://almena.id/schemas/fields/v1/address.json"
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == ["street_address", "postal_code", "locality", "country"]
    assert "ES" in schema["properties"]["country"]["enum"]

    portrait = (await client.get("/schemas/fields/v1/portrait.json")).json()
    assert portrait["properties"]["media_type"] == {"enum": ["image/jpeg", "image/png"]}
    phone = (await client.get("/schemas/fields/v1/phone_number.json")).json()
    assert phone["pattern"] == catalog.E164
    for missing in ("nope.json", "given_name", "street_address.json"):
        assert (await client.get(f"/schemas/fields/v1/{missing}")).status_code == 404


async def test_every_field_has_every_label(db: AsyncSession) -> None:
    def check(item: catalog.Field) -> None:
        assert set(item.labels) == set(catalog.LANGUAGES), item.id
        for part in item.parts:
            check(part.field)

    found = await trust_anchor.load(db)
    assert found.fields
    for item in found.fields.values():
        check(item)
        assert item.category in found.field_categories
        if item.domain:
            assert item.domain in found.domains, item.id
    for domain in found.domains.values():
        for code in domain.codes:
            assert set(code.labels) == set(catalog.LANGUAGES), (domain.id, code.value)
