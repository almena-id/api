from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.test_directory import _sign_in

FIELDS: list[dict[str, Any]] = [
    {"ref": "given_name"},
    {"ref": "family_name"},
    {"ref": "birthdate", "narrow": {"max_date": "2008-10-01"}},
    {"ref": "nationalities", "narrow": {"values": ["PT", "ES"]}},
    {"ref": "address"},
    {"ref": "phone_number", "required": False, "help": {"en": "  "}},
    {
        "ref": "document_file",
        "as": "previous_degree",
        "help": {"en": "Your school diploma", "es": "Tu título de bachillerato"},
        "narrow": {"values": ["pdf"]},
    },
    {"ref": "document_file", "as": "id_scan", "narrow": {"values": ["jpeg", "pdf"]}},
]


async def test_a_member_creates_a_form_from_the_catalogue(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"
    assert (await client.get(base, headers=headers)).json() == []

    response = await client.post(
        base,
        json={
            "name": {"en": " Enrollment request ", "es": "Solicitud de matrícula"},
            "description": {"en": ""},
            "fields": FIELDS,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    form = response.json()
    assert "kind" not in form and form["name"] == {
        "en": "Enrollment request",
        "es": "Solicitud de matrícula",
    }
    assert form["description"] is None and form["slug"].startswith("frm_")
    fields = form["fields"]
    assert fields[0] == {"ref": "given_name", "required": True}
    # Narrowed values come back in the domain's order; blank help is dropped.
    assert fields[3]["narrow"] == {"values": ["ES", "PT"]}
    assert fields[5] == {"ref": "phone_number", "required": False}
    assert fields[7]["narrow"] == {"values": ["pdf", "jpeg"]}
    # Texts by language: trimmed, the empty ones dropped.
    assert fields[6]["help"] == {"en": "Your school diploma", "es": "Tu título de bachillerato"}
    assert form["description"] is None

    assert (await client.get(f"{base}/{form['id']}", headers=headers)).json() == form
    assert [f["id"] for f in (await client.get(base, headers=headers)).json()] == [form["id"]]

    schema = (await client.get(f"{base}/{form['id']}/schema", headers=headers)).json()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Enrollment request"
    assert list(schema["properties"]) == [
        "given_name",
        "family_name",
        "birthdate",
        "nationalities",
        "address",
        "phone_number",
        "previous_degree",
        "id_scan",
    ]
    assert "phone_number" not in schema["required"] and "address" in schema["required"]
    props = schema["properties"]
    assert props["given_name"] == {"$ref": "https://almena.id/schemas/fields/v1/given_name.json"}
    assert props["birthdate"]["formatMaximum"] == "2008-10-01"
    assert props["nationalities"]["items"] == {"enum": ["ES", "PT"]}
    assert props["previous_degree"] == {
        "$ref": "https://almena.id/schemas/fields/v1/document_file.json",
        "description": "Your school diploma",
        "properties": {"media_type": {"enum": ["application/pdf"]}},
    }

    # Nobody outside the tenant sees it.
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(base, headers=eve)).status_code == 404
    assert (await client.get(f"{base}/{form['id']}", headers=eve)).status_code == 404


async def test_a_form_only_takes_catalogue_fields_made_stricter(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"

    async def refused(fields: list[dict[str, Any]]) -> str:
        response = await client.post(
            base, json={"name": {"en": "Request"}, "fields": fields}, headers=headers
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        code: str = detail if isinstance(detail, str) else detail["code"]
        return code

    name = {"ref": "given_name"}
    assert await refused([]) == "fields_required"
    # Tenants do not define fields, nor ask for a group's parts alone.
    assert await refused([{"ref": "favourite_colour"}]) == "field_unknown"
    assert await refused([{"ref": "street_address"}]) == "field_unknown"
    assert await refused([name, name]) == "field_key_duplicate"
    # Which field does not hold: its index, the one repeating a key for a duplicate.
    which = await client.post(
        base,
        json={"name": {"en": "Request"}, "fields": [name, {"ref": "family_name"}, name]},
        headers=headers,
    )
    assert which.json()["detail"] == {"code": "field_key_duplicate", "field": 2}
    assert await refused([{"ref": "document_file"}, {"ref": "document_file"}]) == (
        "field_key_duplicate"
    )
    # Only a repeatable field takes a name of its own, and a good one.
    assert await refused([{**name, "as": "first_name"}]) == "field_rename_invalid"
    assert await refused([{"ref": "document_file", "as": "Degree"}]) == "field_key_invalid"
    # Narrowing only makes stricter, and only what the type allows.
    assert await refused([{**name, "narrow": {"values": ["x"]}}]) == "field_narrow_invalid"
    assert await refused([{**name, "narrow": {"max_length": 500}}]) == "field_narrow_invalid"
    assert await refused([{"ref": "nationalities", "narrow": {"values": ["XX"]}}]) == (
        "field_narrow_invalid"
    )
    assert await refused([{"ref": "nationalities", "narrow": {"values": []}}]) == (
        "field_narrow_invalid"
    )
    assert await refused([{"ref": "sex", "narrow": {"values": ["1"]}}]) == "field_narrow_invalid"
    assert await refused([{"ref": "portrait", "narrow": {"values": ["pdf"]}}]) == (
        "field_narrow_invalid"
    )
    assert await refused(
        [{"ref": "birthdate", "narrow": {"min_date": "2020-01-02", "max_date": "2020-01-01"}}]
    ) == ("field_narrow_invalid")
    ok = await client.post(
        base,
        json={
            "name": {"en": "Request"},
            "fields": [{"ref": "sex", "narrow": {"values": [2, 1]}}],
        },
        headers=headers,
    )
    assert ok.status_code == 201 and ok.json()["fields"][0]["narrow"] == {"values": [1, 2]}


async def test_form_texts_are_by_the_portals_languages(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"
    fields = [{"ref": "given_name"}]
    for name in ({"en": "  "}, {}, {"fr": "Demande"}, "Request"):
        refused = await client.post(base, json={"name": name, "fields": fields}, headers=headers)
        assert refused.status_code == 422, name
    long_help = [{"ref": "given_name", "help": {"es": "x" * 501}}]
    refused = await client.post(
        base, json={"name": {"es": "Solicitud"}, "fields": long_help}, headers=headers
    )
    assert refused.status_code == 422
    made = await client.post(
        base, json={"name": {"es": "Solicitud"}, "fields": fields}, headers=headers
    )
    assert made.status_code == 201 and made.json()["name"] == {"es": "Solicitud"}
