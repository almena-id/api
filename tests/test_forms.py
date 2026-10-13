from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox, bilingual
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
    await bilingual(client, headers, tenant)
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
    assert which.json() == {"detail": "field_key_duplicate", "field": 2}
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
    await bilingual(client, headers, tenant)
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


async def _form(
    client: AsyncClient, headers: dict[str, str], tenant: str, fields: list[dict[str, Any]]
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/tenants/{tenant}/forms",
        json={"name": {"en": "Membership"}, "fields": fields},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    form: dict[str, Any] = response.json()
    return form


async def _offered(client: AsyncClient, headers: dict[str, str], tenant: str, form: str) -> None:
    """An issuer of the tenant offers memberships with the form."""
    base = f"/api/v1/tenants/{tenant}/issuers"
    issuer = (await client.post(base, json={"name": "Club"}, headers=headers)).json()
    saved = await client.put(
        f"{base}/{issuer['id']}/credential-types",
        json={"types": ["membership"], "forms": {"membership": form}},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text


async def test_a_form_not_in_use_changes_freely(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    await bilingual(client, headers, tenant)
    form = await _form(client, headers, tenant, [{"ref": "given_name"}, {"ref": "email"}])
    path = f"/api/v1/tenants/{tenant}/forms/{form['id']}"
    changed = await client.put(
        path,
        json={
            "name": {"en": "Members", "es": "Socios"},
            "fields": [{"ref": "family_name"}, {"ref": "birthdate"}],
        },
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["name"] == {"en": "Members", "es": "Socios"}
    assert [f["ref"] for f in body["fields"]] == ["family_name", "birthdate"]
    assert body["id"] == form["id"] and body["slug"] == form["slug"]
    # The same checks as creating it.
    empty = await client.put(path, json={"name": {"en": "X"}, "fields": []}, headers=headers)
    assert empty.status_code == 422
    assert empty.json()["detail"] == "fields_required"


async def test_a_form_in_use_only_takes_more(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    fields: list[dict[str, Any]] = [
        {"ref": "given_name"},
        {"ref": "birthdate", "narrow": {"max_date": "2008-10-01"}},
        {"ref": "nationalities", "narrow": {"values": ["PT", "ES"]}},
        {"ref": "phone_number", "required": False},
    ]
    form = await _form(client, headers, tenant, fields)
    await _offered(client, headers, tenant, form["id"])
    path = f"/api/v1/tenants/{tenant}/forms/{form['id']}"

    async def put(changed: list[dict[str, Any]]) -> Any:
        return await client.put(
            path, json={"name": {"en": "Membership, again"}, "fields": changed}, headers=headers
        )

    # Wider: a later date, more values, a field no longer required, a new optional one.
    wider: list[dict[str, Any]] = [
        {"ref": "given_name", "required": False},
        {"ref": "birthdate", "narrow": {"max_date": "2010-01-01"}},
        {"ref": "nationalities", "narrow": {"values": ["PT", "ES", "FR"]}},
        {"ref": "phone_number", "required": False},
        {"ref": "email", "required": False},
    ]
    assert (await put(wider)).status_code == 200

    stricter_ones: list[tuple[int, dict[str, Any]]] = [
        (1, {"ref": "birthdate", "narrow": {"max_date": "2005-01-01"}}),
        (2, {"ref": "nationalities", "narrow": {"values": ["PT"]}}),
        (3, {"ref": "phone_number"}),
        (0, {"ref": "given_name", "narrow": {"max_length": 10}}),
    ]
    for index, stricter in stricter_ones:
        changed = [dict(field) for field in wider]
        changed[index] = stricter
        refused = await put(changed)
        assert refused.status_code == 409, stricter
        assert refused.json() == {"detail": "form_in_use", "field": index}

    required_new = await put([*wider, {"ref": "family_name"}])
    assert required_new.json() == {"detail": "form_in_use", "field": 5}
    removed = await put(wider[1:])
    assert removed.status_code == 409
    assert removed.json()["detail"] == "form_in_use"


async def test_a_form_is_deleted_until_used(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"
    spare = await _form(client, headers, tenant, [{"ref": "given_name"}])
    assert (await client.delete(f"{base}/{spare['id']}", headers=headers)).status_code == 204
    assert (await client.get(f"{base}/{spare['id']}", headers=headers)).status_code == 404

    used = await _form(client, headers, tenant, [{"ref": "given_name"}])
    assert used["in_use"] is False
    await _offered(client, headers, tenant, used["id"])
    assert (await client.get(f"{base}/{used['id']}", headers=headers)).json()["in_use"] is True
    refused = await client.delete(f"{base}/{used['id']}", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "form_in_use"


async def test_another_tenants_form_is_out_of_reach(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@acme.com")
    bob, _ = await _sign_in(client, outbox, "bob@acme.com")
    form = await _form(client, ada, tenant, [{"ref": "given_name"}])
    path = f"/api/v1/tenants/{tenant}/forms/{form['id']}"
    assert (await client.delete(path, headers=bob)).status_code == 404
    body = {"name": {"en": "Mine"}, "fields": [{"ref": "email"}]}
    assert (await client.put(path, json=body, headers=bob)).status_code == 404


async def test_credentials_of_a_form_in_use_only_take_more(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    created = await client.post(
        f"/api/v1/tenants/{tenant}/forms",
        json={"name": {"en": "Membership"}, "credentials": [{"type": "verified_email"}]},
        headers=headers,
    )
    form = created.json()
    await _offered(client, headers, tenant, form["id"])
    path = f"/api/v1/tenants/{tenant}/forms/{form['id']}"

    async def put(credentials: list[dict[str, Any]]) -> Any:
        body = {"name": {"en": "Membership"}, "credentials": credentials}
        return await client.put(path, json=body, headers=headers)

    optional = {"type": "verified_email", "required": False, "purpose": {"en": "To write"}}
    assert (await put([optional])).status_code == 200
    refused = await put([optional, {"type": "verified_phone"}])
    assert refused.json() == {"detail": "form_in_use", "credential": 1}
    fewer = {**optional, "claims": [form["credentials"][0]["claims"][0]]}
    if fewer["claims"] != form["credentials"][0]["claims"]:
        assert (await put([fewer])).json() == {"detail": "form_in_use", "credential": 0}
    assert (await put([optional, {"type": "verified_phone", "required": False}])).status_code == 200
