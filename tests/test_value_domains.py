from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.subscriptions import ANCHOR, paid_sign_in
from tests.test_directory import _sign_in

BLOOD: dict[str, Any] = {
    "key": "blood_type",
    "labels": {"en": "Blood types", "es": "Grupos sanguíneos"},
    "source": "ISBT 128",
    "codes": [
        {"value": "A", "labels": {"en": "A", "es": "A"}},
        {"value": "B", "labels": {"en": "B", "es": "B"}},
    ],
}


async def _post(
    client: AsyncClient, headers: dict[str, str], url: str, body: dict[str, Any]
) -> Any:
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_anchor_keeps_the_value_domains(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/value-domains"
    seeded = {item["key"]: item for item in (await client.get(base, headers=headers)).json()}
    assert len(seeded["country"]["codes"]) == 249 and seeded["country"]["uses"] > 0

    blood = await _post(client, headers, base, BLOOD)
    assert blood["uses"] == 0
    published = (await client.get("/api/v1/catalog/fields")).json()["domains"]
    assert [code["value"] for code in published["blood_type"]["codes"]] == ["A", "B"]

    # A field draws on it — the anchor's, or any tenant's own.
    field = await _post(
        client,
        headers,
        f"/api/v1/tenants/{anchor}/fields",
        {
            "key": "blood_group",
            "type": "code",
            "labels": {"en": "Blood group", "es": "Grupo sanguíneo"},
            "domain": "blood_type",
            "category": "person",
            "source": "ISBT 128",
        },
    )
    assert field["field"]["domain"] == "blood_type"
    schema = (await client.get("/schemas/fields/v1/blood_group.json")).json()
    assert schema["enum"] == ["A", "B"]

    # In use, it only grows: renamed and added to, nothing taken away.
    url = f"{base}/{blood['id']}"
    grown = [
        {"value": "A", "labels": {"en": "Group A", "es": "Grupo A"}},
        {"value": "B", "labels": {"en": "B", "es": "B"}},
        {"value": "AB", "labels": {"en": "AB", "es": "AB"}},
    ]
    added = await client.patch(url, json={"codes": grown}, headers=headers)
    assert added.status_code == 200 and added.json()["uses"] == 1
    schema = (await client.get("/schemas/fields/v1/blood_group.json")).json()
    assert schema["enum"] == ["A", "B", "AB"]
    shrunk = await client.patch(url, json={"codes": grown[1:]}, headers=headers)
    assert shrunk.status_code == 409 and shrunk.json()["detail"] == "domain_in_use"
    kept = await client.delete(url, headers=headers)
    assert kept.status_code == 409 and kept.json()["detail"] == "domain_in_use"
    renamed = await client.patch(
        url, json={"labels": {"en": "Blood", "es": "Sangre"}, "source": "ISBT"}, headers=headers
    )
    assert renamed.json()["labels"]["en"] == "Blood" and renamed.json()["source"] == "ISBT"

    # Unused, it changes freely and goes.
    await client.delete(f"/api/v1/tenants/{anchor}/fields/{field['id']}", headers=headers)
    assert (await client.patch(url, json={"codes": grown[1:]}, headers=headers)).status_code == 200
    assert (await client.delete(url, headers=headers)).status_code == 204
    # The file formats never go: every file field draws on them.
    formats = await client.delete(f"{base}/{seeded['file_format']['id']}", headers=headers)
    assert formats.status_code == 409


async def test_its_value_domains_are_checked(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/value-domains"
    one = {"value": "x", "labels": {"en": "X", "es": "X"}}
    for change, code in (
        ({"key": "Blood"}, "key_invalid"),
        ({"labels": {"en": "Blood"}}, "labels_required"),
        ({"source": " "}, "source_required"),
        ({"codes": []}, "codes_invalid"),
        ({"codes": [one, one]}, "codes_invalid"),
        ({"codes": [one, {"value": 1, "labels": {"en": "1", "es": "1"}}]}, "codes_invalid"),
        ({"codes": [{"value": "x", "labels": {"en": "X"}}]}, "codes_invalid"),
        ({"codes": [{**one, "media_type": "text/plain"}]}, "media_type_invalid"),
    ):
        response = await client.post(base, json={**BLOOD, **change}, headers=headers)
        assert response.status_code == 422, (change, response.text)
        assert response.json()["detail"] == code, change
    taken = await client.post(base, json={**BLOOD, "key": "country"}, headers=headers)
    assert taken.status_code == 409 and taken.json()["detail"] == "key_exists"
    numbers = await _post(
        client,
        headers,
        base,
        {**BLOOD, "key": "levels", "codes": [{"value": 1, "labels": {"en": "1", "es": "1"}}]},
    )
    assert numbers["codes"][0]["value"] == 1


async def test_tenants_build_on_them_and_never_change_them(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    domains = (await client.get(f"/api/v1/tenants/{anchor}/value-domains", headers=headers)).json()
    country = next(item for item in domains if item["key"] == "country")

    ada, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    own = f"/api/v1/tenants/{tenant}"
    made = await _post(
        client,
        ada,
        f"{own}/fields",
        {"key": "home_country", "type": "code", "labels": {"en": "Home"}, "domain": "country"},
    )
    assert made["field"]["domain"] == "country" and len(made["field"].get("codes", [])) == 0
    for body in (
        {"key": "f1", "type": "code", "labels": {"en": "F"}, "domain": "nowhere"},
        {"key": "f2", "type": "code", "labels": {"en": "F"}, "domain": "file_format"},
        {
            "key": "f3",
            "type": "code",
            "labels": {"en": "F"},
            "domain": "country",
            "options": [
                {"value": "a", "labels": {"en": "A"}},
                {"value": "b", "labels": {"en": "B"}},
            ],
        },
    ):
        refused = await client.post(f"{own}/fields", json=body, headers=ada)
        assert refused.status_code == 422 and refused.json()["detail"] == "domain_invalid"
    # Asked for by a form, it keeps its value domain.
    await _post(
        client,
        ada,
        f"{own}/forms",
        {"name": {"en": "x"}, "fields": [{"ref": "custom:home_country"}], "credentials": []},
    )
    url = f"{own}/fields/{made['id']}"
    options = [{"value": "a", "labels": {"en": "A"}}, {"value": "b", "labels": {"en": "B"}}]
    moved = await client.patch(url, json={"type": "code", "options": options}, headers=ada)
    assert moved.status_code == 409 and moved.json()["detail"] == "field_in_use"
    same = await client.patch(url, json={"type": "code", "domain": "country"}, headers=ada)
    assert same.status_code == 200

    # Its field counts as a use of the anchor's domain.
    listed = (await client.get(f"/api/v1/tenants/{anchor}/value-domains", headers=headers)).json()
    assert next(item for item in listed if item["key"] == "country")["uses"] == country["uses"] + 1

    attempts: list[tuple[str, str, Any]] = [
        ("GET", f"{own}/value-domains", None),
        ("POST", f"{own}/value-domains", BLOOD),
        ("PATCH", f"{own}/value-domains/{country['id']}", {"source": "x"}),
        ("DELETE", f"{own}/value-domains/{country['id']}", None),
    ]
    for method, url, body in attempts:
        response = await client.request(method, url, json=body, headers=ada)
        assert response.status_code == 403 and response.json()["detail"] == "anchor_only"
