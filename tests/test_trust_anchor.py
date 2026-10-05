from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.subscriptions import paid_sign_in
from tests.test_directory import _sign_in

ANCHOR = "anchor@example.net"
BADGE: dict[str, Any] = {
    "key": "badge_number",
    "type": "text",
    "labels": {"en": "Badge number", "es": "Número de credencial"},
    "max_length": 20,
    "category": "membership",
    "source": "schema.org identifier",
}
VISIT: dict[str, Any] = {
    "key": "visitor_pass",
    "labels": {"en": "Visitor pass", "es": "Pase de visitante"},
    "descriptions": {"en": "Who may visit, and with which badge.", "es": "Quién puede visitar."},
    "category": "membership",
    "source": "Almena",
    "claims": [{"field": "given_name"}, {"field": "badge_number", "required": False}],
    "w3c_type": "VisitorPassCredential",
}


async def _post(
    client: AsyncClient, headers: dict[str, str], url: str, body: dict[str, Any]
) -> Any:
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _form(client: AsyncClient, headers: dict[str, str], tenant: str, ref: str) -> Any:
    body = {"name": {"en": "Visit"}, "fields": [{"ref": ref}], "credentials": []}
    return await _post(client, headers, f"/api/v1/tenants/{tenant}/forms", body)


async def test_the_anchor_is_the_root_and_says_so(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    tenants = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert tenants[0]["name"] == "Almena Trust Anchor" and tenants[0]["anchor"] is True
    other, _ = await paid_sign_in(client, outbox, "ada@acme.com")
    assert (await client.get("/api/v1/tenants", headers=other)).json()[0]["anchor"] is False
    assert (await client.get(f"/api/v1/tenants/{anchor}", headers=headers)).json()["anchor"]


async def test_its_fields_are_everyones(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/fields"
    seeded = (await client.get(base, headers=headers)).json()
    assert {"given_name", "address", "document_file"} <= {item["ref"] for item in seeded}

    badge = await _post(client, headers, base, BADGE)
    # Referred to by its key, published, with its category and standard.
    assert badge["ref"] == "badge_number"
    field = badge["field"]
    assert field["category"] == "membership" and field["source"] == "schema.org identifier"
    assert field["schema"] == "https://almena.id/schemas/fields/v1/badge_number.json"
    published = (await client.get("/api/v1/catalog/fields")).json()["fields"]
    assert published[-1]["id"] == "badge_number"
    schema = (await client.get("/schemas/fields/v1/badge_number.json")).json()
    assert schema["maxLength"] == 20 and schema["title"] == "Badge number"

    # Any tenant's forms ask for it by its key; nobody takes the key.
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    await _form(client, other, tenant, "badge_number")
    own = f"/api/v1/tenants/{tenant}/fields"
    taken = await client.post(own, json={**BADGE, "category": None, "source": None}, headers=other)
    assert taken.status_code == 422 and taken.json()["detail"] == "key_reserved"

    # Used by a form, it stays.
    found = next(item for item in (await client.get(base, headers=headers)).json())
    assert found["key"] == "badge_number"
    gone = await client.delete(f"{base}/{found['id']}", headers=headers)
    assert gone.status_code == 409 and gone.json()["detail"] == "field_in_use"


async def test_its_fields_say_more(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/fields"
    for change, code in (
        ({"labels": {"en": "Badge number"}}, "labels_required"),
        ({"category": "nowhere"}, "category_invalid"),
        ({"category": None}, "category_invalid"),
        ({"source": "  "}, "source_required"),
        ({"key": "given_name"}, None),
    ):
        response = await client.post(base, json={**BADGE, **change}, headers=headers)
        if code is None:
            assert response.status_code == 409 and response.json()["detail"] == "key_exists"
        else:
            assert response.status_code == 422 and response.json()["detail"] == code, change

    # Categories and standards are the anchor's alone.
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    own = f"/api/v1/tenants/{tenant}/fields"
    for change in ({"source": None}, {"category": None}):
        body = {**BADGE, "key": "badge", **change}
        response = await client.post(own, json=body, headers=other)
        assert response.status_code == 422 and response.json()["detail"] == "anchor_only"


async def test_it_keeps_the_credential_types(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    await _post(client, headers, f"/api/v1/tenants/{anchor}/fields", BADGE)
    base = f"/api/v1/tenants/{anchor}/credential-types"
    seeded = (await client.get(base, headers=headers)).json()
    assert {"pid", "membership"} <= {item["key"] for item in seeded}

    created = await _post(client, headers, base, VISIT)
    assert created["slug"].startswith("cty_")
    kind = created["type"]
    vct = "https://almena.id/credentials/visitor_pass/v1"
    assert kind["formats"]["dc+sd-jwt"] == {"vct": vct}
    assert kind["claims"] == [
        {"field": "given_name", "name": "given_name", "required": True},
        {"field": "badge_number", "name": "badge_number", "required": False},
    ]
    published = (await client.get("/api/v1/catalog/credentials")).json()["types"]
    assert published[-1]["id"] == "visitor_pass"
    metadata = (await client.get("/.well-known/vct/credentials/visitor_pass/v1")).json()
    assert metadata["vct"] == vct and metadata["claims"][1]["path"] == ["badge_number"]
    schema = (await client.get("/schemas/credentials/v1/visitor_pass.json")).json()
    assert schema["required"] == ["given_name"]

    # Any tenant's issuers grant it.
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    issuer = await _post(
        client,
        other,
        f"/api/v1/tenants/{tenant}/issuers",
        {"name": "Club", "description": {"en": "x"}},
    )
    granted = await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer['id']}/credential-types",
        json={"types": ["visitor_pass"]},
        headers=other,
    )
    assert granted.status_code == 200, granted.text

    # Granted, it stays; nor does its field go.
    gone = await client.delete(f"{base}/{created['id']}", headers=headers)
    assert gone.status_code == 409 and gone.json()["detail"] == "credential_type_in_use"
    fields = (await client.get(f"/api/v1/tenants/{anchor}/fields", headers=headers)).json()
    badge = next(item for item in fields if item["key"] == "badge_number")
    kept = await client.delete(f"/api/v1/tenants/{anchor}/fields/{badge['id']}", headers=headers)
    assert kept.status_code == 409 and kept.json()["detail"] == "field_in_use"

    await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer['id']}/credential-types",
        json={"types": []},
        headers=other,
    )
    gone = await client.delete(f"{base}/{created['id']}", headers=headers)
    assert gone.status_code == 204
    assert (await client.get("/.well-known/vct/credentials/visitor_pass/v1")).status_code == 404


async def test_its_types_are_checked(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    own = await _post(
        client,
        headers,
        f"/api/v1/tenants/{anchor}/fields",
        {**BADGE},
    )
    assert own["ref"] == "badge_number"
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    await _post(
        client,
        other,
        f"/api/v1/tenants/{tenant}/fields",
        {"key": "locker", "type": "text", "labels": {"en": "Locker"}},
    )
    base = f"/api/v1/tenants/{anchor}/credential-types"
    for change, code in (
        ({"key": "Visitor"}, "key_invalid"),
        ({"labels": {"en": "Visitor pass"}}, "labels_required"),
        ({"descriptions": {"en": "x", "es": " "}}, "descriptions_required"),
        ({"category": "person"}, "category_invalid"),
        ({"source": " "}, "source_required"),
        ({"claims": []}, "claims_invalid"),
        ({"claims": [{"field": "given_name"}, {"field": "given_name"}]}, "claims_invalid"),
        ({"claims": [{"field": "custom:locker"}]}, "claims_invalid"),
        ({"claims": [{"field": "nothing"}]}, "claims_invalid"),
        ({"claims": [{"field": "document_file"}]}, "claims_invalid"),
        ({"vct": "urn:example:visit:1"}, "vct_invalid"),
        ({"issuance": "external"}, "vct_invalid"),
        ({"issuance": "external", "vct": "not a uri"}, "vct_invalid"),
        ({"w3c_type": "visitor pass"}, "w3c_type_invalid"),
        ({"w3c_type": "MembershipCredential"}, "w3c_type_invalid"),
        ({"mdoc_doctype": "Visitor"}, "mdoc_doctype_invalid"),
    ):
        response = await client.post(base, json={**VISIT, **change}, headers=headers)
        assert response.status_code == 422, (change, response.text)
        assert response.json()["detail"] == code, change
    taken = await client.post(base, json={**VISIT, "key": "membership"}, headers=headers)
    assert taken.status_code == 409 and taken.json()["detail"] == "key_exists"

    external = await _post(
        client,
        headers,
        base,
        {
            **VISIT,
            "key": "visa",
            "issuance": "external",
            "vct": "urn:example:visa:1",
            "w3c_type": None,
            "mdoc_doctype": "org.example.visa.1",
        },
    )
    assert external["type"]["formats"] == {
        "dc+sd-jwt": {"vct": "urn:example:visa:1"},
        "mso_mdoc": {"doctype": "org.example.visa.1"},
    }
    # Issued elsewhere: no tenant's issuer grants it.
    issuer = await _post(
        client,
        other,
        f"/api/v1/tenants/{tenant}/issuers",
        {"name": "Club", "description": {"en": "x"}},
    )
    refused = await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer['id']}/credential-types",
        json={"types": ["visa"]},
        headers=other,
    )
    assert refused.status_code == 422


async def test_a_tenants_field_is_edited_while_nothing_breaks(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/fields"
    campus = await _post(
        client,
        headers,
        base,
        {
            "key": "campus",
            "type": "code",
            "labels": {"en": "Campus"},
            "options": [
                {"value": "north", "labels": {"en": "North"}},
                {"value": "south", "labels": {"en": "South"}},
            ],
        },
    )
    url = f"{base}/{campus['id']}"

    # Unused, anything but its key changes.
    text = await client.patch(url, json={"type": "text", "max_length": 10}, headers=headers)
    assert text.status_code == 200, text.text
    assert text.json()["field"]["type"] == "text" and text.json()["ref"] == "custom:campus"
    assert text.json()["field"]["max_length"] == 10
    options = [
        {"value": "north", "labels": {"en": "North"}},
        {"value": "south", "labels": {"en": "South"}},
    ]
    await client.patch(url, json={"type": "code", "options": options}, headers=headers)

    # Asked for by a form, it only takes more.
    await _form(client, headers, tenant, "custom:campus")
    renamed = await client.patch(url, json={"labels": {"en": "Site"}}, headers=headers)
    assert renamed.status_code == 200 and renamed.json()["field"]["labels"] == {"en": "Site"}
    more = [*options, {"value": "east", "labels": {"en": "East"}}]
    added = await client.patch(url, json={"type": "code", "options": more}, headers=headers)
    assert added.status_code == 200, added.text
    for change in (
        {"type": "code", "options": options},
        {"type": "codes", "options": more},
        {"type": "text"},
    ):
        refused = await client.patch(url, json=change, headers=headers)
        assert refused.status_code == 409 and refused.json()["detail"] == "field_in_use", change
    filed = await client.patch(url, json={"category": "membership"}, headers=headers)
    assert filed.status_code == 422 and filed.json()["detail"] == "anchor_only"


async def test_a_used_text_field_grows_but_never_shrinks(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/fields"
    made = await _post(
        client,
        headers,
        base,
        {"key": "locker", "type": "text", "labels": {"en": "Locker"}, "max_length": 10},
    )
    url = f"{base}/{made['id']}"
    await _form(client, headers, tenant, "custom:locker")
    for change, code in (
        ({"type": "text", "max_length": 20}, 200),
        ({"type": "text"}, 200),
        ({"type": "text", "max_length": 5}, 409),
        ({"type": "text", "pattern": "[0-9]+"}, 409),
    ):
        response = await client.patch(url, json=change, headers=headers)
        assert response.status_code == code, (change, response.text)


async def test_the_anchor_edits_its_fields(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/fields"
    fields = {item["key"]: item for item in (await client.get(base, headers=headers)).json()}

    assert fields["academic_year"]["definition"]["pattern"] == "^[0-9]{4}-[0-9]{4}$"
    given = f"{base}/{fields['given_name']['id']}"
    edited = await client.patch(
        given,
        json={"labels": {"en": "First name", "es": "Nombre"}, "source": "OIDC"},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    published = (await client.get("/api/v1/catalog/fields")).json()["fields"]
    first = next(item for item in published if item["id"] == "given_name")
    assert first["labels"]["en"] == "First name" and first["source"] == "OIDC"
    for change, code in (
        ({"labels": {"en": "Given name"}}, "labels_required"),
        ({"category": "nowhere"}, "category_invalid"),
        ({"source": " "}, "source_required"),
    ):
        response = await client.patch(given, json=change, headers=headers)
        assert response.status_code == 422 and response.json()["detail"] == code, change

    # In use, a group keeps its parts and a field its value domain; their
    # words change.
    for key, status, code in (
        ("address", 409, "field_in_use"),
        ("nationalities", 409, "field_in_use"),
    ):
        url = f"{base}/{fields[key]['id']}"
        refused = await client.patch(url, json={"type": "text"}, headers=headers)
        assert refused.status_code == status and refused.json()["detail"] == code
        relabelled = await client.patch(
            url, json={"labels": {"en": "Where", "es": "Dónde"}}, headers=headers
        )
        assert relabelled.status_code == 200

    # A claim of its types: it takes more, never less.
    member = f"{base}/{fields['member_number']['id']}"
    longer = await client.patch(member, json={"type": "text", "max_length": 80}, headers=headers)
    assert longer.status_code == 200
    shorter = await client.patch(member, json={"type": "text", "max_length": 5}, headers=headers)
    assert shorter.status_code == 409 and shorter.json()["detail"] == "field_in_use"


async def test_the_anchor_edits_its_types(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    await _post(client, headers, f"/api/v1/tenants/{anchor}/fields", BADGE)
    base = f"/api/v1/tenants/{anchor}/credential-types"
    made = await _post(client, headers, base, VISIT)
    url = f"{base}/{made['id']}"

    # Unused, anything but its key changes.
    free = await client.patch(
        url,
        json={
            "labels": {"en": "Guest pass", "es": "Pase de invitado"},
            "claims": [{"field": "family_name"}],
            "w3c_type": "GuestPassCredential",
        },
        headers=headers,
    )
    assert free.status_code == 200, free.text
    kind = free.json()["type"]
    assert kind["labels"]["en"] == "Guest pass" and free.json()["key"] == "visitor_pass"
    assert kind["claims"] == [{"field": "family_name", "name": "family_name", "required": True}]
    assert kind["formats"]["jwt_vc_json"]["type"][1] == "GuestPassCredential"
    for change, status, code in (
        ({"w3c_type": "MembershipCredential"}, 422, "w3c_type_invalid"),
        ({"claims": [{"field": "document_file"}]}, 422, "claims_invalid"),
        ({"vct": "urn:example:guest:1"}, 422, "vct_invalid"),
        ({"labels": {"en": "Guest"}}, 422, "labels_required"),
    ):
        response = await client.patch(url, json=change, headers=headers)
        assert response.status_code == status and response.json()["detail"] == code, change
    assert (await client.patch(url, json={"labels": None}, headers=headers)).status_code == 422

    # Granted by an issuer, its words change; what it carries only grows.
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    issuer = await _post(
        client,
        other,
        f"/api/v1/tenants/{tenant}/issuers",
        {"name": "Club", "description": {"en": "x"}},
    )
    await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer['id']}/credential-types",
        json={"types": ["visitor_pass"]},
        headers=other,
    )
    changes: list[tuple[dict[str, Any], int]] = [
        ({"descriptions": {"en": "Who visits.", "es": "Quién visita."}}, 200),
        ({"category": "work", "source": "Almena, v2"}, 200),
        (
            {"claims": [{"field": "family_name"}, {"field": "badge_number", "required": False}]},
            200,
        ),
        ({"claims": [{"field": "badge_number", "required": False}]}, 409),
        ({"claims": [{"field": "family_name"}, {"field": "badge_number"}]}, 409),
        (
            {
                "claims": [
                    {"field": "family_name"},
                    {"field": "badge_number", "required": False},
                    {"field": "given_name"},
                ]
            },
            409,
        ),
        ({"w3c_type": "VisitCredential"}, 409),
        ({"mdoc_doctype": "org.example.visit.1"}, 409),
    ]
    for change, status in changes:
        response = await client.patch(url, json=change, headers=headers)
        assert response.status_code == status, (change, response.text)
        if status == 409:
            assert response.json()["detail"] == "credential_type_in_use"

    # Another tenant does not reach the anchor's.
    refused = await client.patch(
        f"/api/v1/tenants/{tenant}/credential-types/{made['id']}",
        json={"labels": {"en": "x", "es": "x"}},
        headers=other,
    )
    assert refused.status_code == 404 and refused.json()["detail"] == "credential_type_not_found"
