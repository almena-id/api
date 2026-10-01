from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.test_directory import _sign_in

CAMPUS: dict[str, Any] = {
    "key": "campus",
    "type": "code",
    "labels": {"en": " Campus ", "es": "Campus"},
    "options": [
        {"value": "north", "labels": {"en": "North", "es": "Norte"}},
        {"value": "south", "labels": {"es": "Sur"}},
    ],
}


async def _add(
    client: AsyncClient, headers: dict[str, str], base: str, body: dict[str, Any]
) -> Any:
    response = await client.post(base, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_tenant_adds_its_own_fields_and_forms_use_them(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/fields"
    assert (await client.get(base, headers=headers)).json() == []

    campus = await _add(client, headers, base, CAMPUS)
    assert campus["key"] == "campus" and campus["ref"] == "custom:campus"
    assert campus["slug"].startswith("fld_")
    field = campus["field"]
    assert field["id"] == "custom:campus" and field["category"] == "custom"
    assert field["labels"] == {"en": "Campus", "es": "Campus"}
    assert field["narrowing"] == ["values"] and "schema" not in field
    assert [code["value"] for code in field["codes"]] == ["north", "south"]
    badge = await _add(
        client,
        headers,
        base,
        {
            "key": "locker_number",
            "type": "text",
            "labels": {"es": "Número de taquilla"},
            "max_length": 12,
            "pattern": "[0-9]+",
        },
    )
    assert badge["field"]["max_length"] == 12 and badge["field"]["pattern"] == "[0-9]+"
    proof = await _add(
        client,
        headers,
        base,
        {"key": "proof", "type": "file", "labels": {"en": "Proof"}, "formats": ["png", "pdf"]},
    )
    assert proof["field"]["values"] == ["pdf", "png"]
    keys = [item["key"] for item in (await client.get(base, headers=headers)).json()]
    assert sorted(keys) == ["campus", "locker_number", "proof"]

    forms = f"/api/v1/tenants/{tenant}/forms"
    response = await client.post(
        forms,
        json={
            "name": {"en": "Membership"},
            "fields": [
                {"ref": "given_name"},
                {"ref": "custom:campus", "narrow": {"values": ["south"]}},
                {"ref": "custom:locker_number", "required": False},
                {"ref": "custom:proof", "narrow": {"values": ["pdf"]}},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    form = response.json()
    schema = (await client.get(f"{forms}/{form['id']}/schema", headers=headers)).json()
    props = schema["properties"]
    assert list(props) == ["given_name", "campus", "locker_number", "proof"]
    # The tenant's own fields are written out whole, never published.
    assert props["campus"] == {"title": "Campus", "enum": ["south"]}
    assert props["locker_number"]["maxLength"] == 12
    assert props["locker_number"]["title"] == "Número de taquilla"
    assert props["proof"]["properties"]["media_type"] == {"enum": ["application/pdf"]}
    assert schema["required"] == ["given_name", "campus", "proof"]

    # Narrowing stays within its options; unknown custom refs are refused.
    for fields, detail in (
        ([{"ref": "custom:campus", "narrow": {"values": ["east"]}}], "field_narrow_invalid"),
        ([{"ref": "custom:nope"}], "field_unknown"),
        ([{"ref": "custom:proof", "narrow": {"values": ["jpeg"]}}], "field_narrow_invalid"),
    ):
        bad = await client.post(
            forms,
            json={"name": {"en": "X"}, "fields": fields},
            headers=headers,
        )
        assert bad.status_code == 422 and bad.json()["detail"] == detail

    # A field a form uses stays; one no form uses goes.
    used = await client.delete(f"{base}/{campus['id']}", headers=headers)
    assert used.status_code == 409 and used.json()["detail"] == "field_in_use"
    spare = await _add(
        client, headers, base, {"key": "spare", "type": "date", "labels": {"en": "Spare"}}
    )
    assert (await client.delete(f"{base}/{spare['id']}", headers=headers)).status_code == 204

    # Another tenant neither sees them nor can use them.
    eve, other = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(base, headers=eve)).status_code == 404
    theirs = await client.post(
        f"/api/v1/tenants/{other}/forms",
        json={"name": {"en": "X"}, "fields": [{"ref": "custom:campus"}]},
        headers=eve,
    )
    assert theirs.status_code == 422 and theirs.json()["detail"] == "field_unknown"


async def test_a_custom_field_must_hold(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/fields"

    async def refused(body: dict[str, Any], code: int = 422) -> str:
        response = await client.post(base, json=body, headers=headers)
        assert response.status_code == code, response.text
        detail: str = response.json()["detail"]
        return detail

    text = {"key": "nickname", "type": "text", "labels": {"en": "Nickname"}}
    assert await refused({**text, "key": "Nick name"}) == "key_invalid"
    # Almena's ids are Almena's.
    assert await refused({**text, "key": "given_name"}) == "key_reserved"
    assert await refused({**text, "labels": {"en": "  "}}) == "labels_required"
    assert await refused({**text, "labels": {"fr": "Surnom"}}) == "labels_required"
    assert await refused({**text, "options": CAMPUS["options"]}) == "constraint_invalid"
    assert await refused({**text, "type": "date", "max_length": 3}) == "constraint_invalid"
    assert await refused({**text, "pattern": "[a-"}) == "pattern_invalid"
    assert await refused({**CAMPUS, "options": CAMPUS["options"][:1]}) == "options_invalid"
    assert await refused({**CAMPUS, "options": [CAMPUS["options"][0], CAMPUS["options"][0]]}) == (
        "options_invalid"
    )
    assert await refused({**CAMPUS, "options": None}) == "options_invalid"
    assert await refused({**text, "type": "file"}) == "formats_invalid"
    assert await refused({**text, "type": "file", "formats": ["exe"]}) == "formats_invalid"
    await _add(client, headers, base, text)
    assert await refused(text, code=409) == "key_exists"
