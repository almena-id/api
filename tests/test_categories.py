from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.subscriptions import ANCHOR, paid_sign_in
from tests.test_directory import _sign_in

LABELS = {"en": "Health", "es": "Salud"}


async def _post(
    client: AsyncClient, headers: dict[str, str], url: str, body: dict[str, Any]
) -> Any:
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_anchor_keeps_the_categories(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/categories"
    seeded = (await client.get(base, headers=headers)).json()
    assert {(item["kind"], item["key"]) for item in seeded} >= {
        ("field", "person"),
        ("credential", "identity"),
    }
    person = next(item for item in seeded if item["key"] == "person")
    assert person["uses"] > 0

    # A field category: filed under, published, then emptied and deleted.
    health = await _post(
        client, headers, base, {"kind": "field", "key": "health", "labels": LABELS}
    )
    assert health["uses"] == 0
    published = (await client.get("/api/v1/catalog/fields")).json()["categories"]
    assert published[-1] == {"id": "health", "labels": LABELS}
    field = await _post(
        client,
        headers,
        f"/api/v1/tenants/{anchor}/fields",
        {
            "key": "blood_type",
            "type": "text",
            "labels": {"en": "Blood type", "es": "Grupo sanguíneo"},
            "category": "health",
            "source": "Almena",
        },
    )
    url = f"{base}/{health['id']}"
    renamed = await client.patch(
        url, json={"labels": {"en": "Health care", "es": "Sanidad"}}, headers=headers
    )
    assert renamed.status_code == 200 and renamed.json()["uses"] == 1
    kept = await client.delete(url, headers=headers)
    assert kept.status_code == 409 and kept.json()["detail"] == "category_in_use"
    await client.delete(f"/api/v1/tenants/{anchor}/fields/{field['id']}", headers=headers)
    assert (await client.delete(url, headers=headers)).status_code == 204
    published = (await client.get("/api/v1/catalog/fields")).json()["categories"]
    assert "health" not in [item["id"] for item in published]

    # The same key may name a category of each kind, once.
    same = await _post(
        client, headers, base, {"kind": "credential", "key": "person", "labels": LABELS}
    )
    assert same["kind"] == "credential"
    taken = await client.post(
        base, json={"kind": "credential", "key": "person", "labels": LABELS}, headers=headers
    )
    assert taken.status_code == 409 and taken.json()["detail"] == "key_exists"
    for body, code in (
        ({"kind": "field", "key": "Health", "labels": LABELS}, "key_invalid"),
        ({"kind": "field", "key": "care", "labels": {"en": "Care"}}, "labels_required"),
    ):
        response = await client.post(base, json=body, headers=headers)
        assert response.status_code == 422 and response.json()["detail"] == code


async def test_a_credential_category_any_tenant_files_under_stays(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/categories"
    club = await _post(
        client, headers, base, {"kind": "credential", "key": "clubs", "labels": LABELS}
    )
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    await _post(
        client,
        other,
        f"/api/v1/tenants/{tenant}/credential-types",
        {
            "key": "card",
            "labels": {"en": "Club card"},
            "descriptions": {"en": "A member."},
            "category": "clubs",
            "claims": [{"field": "given_name"}],
        },
    )
    kept = await client.delete(f"{base}/{club['id']}", headers=headers)
    assert kept.status_code == 409 and kept.json()["detail"] == "category_in_use"


async def test_what_the_anchor_keeps_no_other_tenant_changes(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    fields = (await client.get(f"/api/v1/tenants/{anchor}/fields", headers=headers)).json()
    types = (await client.get(f"/api/v1/tenants/{anchor}/credential-types", headers=headers)).json()
    categories = (await client.get(f"/api/v1/tenants/{anchor}/categories", headers=headers)).json()
    field, kind, category = fields[0]["id"], types[0]["id"], categories[0]["id"]

    # Even subscribed, another tenant reaches none of it: not by its own path…
    other, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    own = f"/api/v1/tenants/{tenant}"
    for method, url, body in (
        ("PATCH", f"{own}/fields/{field}", {"labels": {"en": "x"}}),
        ("DELETE", f"{own}/fields/{field}", None),
        ("PATCH", f"{own}/credential-types/{kind}", {"labels": {"en": "x"}}),
        ("DELETE", f"{own}/credential-types/{kind}", None),
    ):
        response = await client.request(method, url, json=body, headers=other)
        assert response.status_code == 404, (method, url, response.text)
    attempts: list[tuple[str, str, Any]] = [
        ("GET", f"{own}/categories", None),
        ("POST", f"{own}/categories", {"kind": "field", "key": "x", "labels": LABELS}),
        ("PATCH", f"{own}/categories/{category}", {"labels": LABELS}),
        ("DELETE", f"{own}/categories/{category}", None),
    ]
    for method, url, body in attempts:
        response = await client.request(method, url, json=body, headers=other)
        assert response.status_code == 403 and response.json()["detail"] == "anchor_only"

    # …nor by the anchor's, which is not its tenant.
    for method, url in (
        ("PATCH", f"/api/v1/tenants/{anchor}/fields/{field}"),
        ("PATCH", f"/api/v1/tenants/{anchor}/categories/{category}"),
        ("DELETE", f"/api/v1/tenants/{anchor}/credential-types/{kind}"),
    ):
        response = await client.request(method, url, json={"labels": LABELS}, headers=other)
        assert response.status_code in (403, 404), (method, url)
        assert response.status_code != 200


async def test_what_a_tenant_makes_is_its_own(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    made = await _post(
        client,
        ada,
        f"/api/v1/tenants/{tenant}/fields",
        {"key": "locker", "type": "text", "labels": {"en": "Locker"}},
    )
    bob, elsewhere = await paid_sign_in(client, outbox, "bob@other.org")
    # Not listed, not reached, not usable by another tenant.
    assert (await client.get(f"/api/v1/tenants/{elsewhere}/fields", headers=bob)).json() == []
    reached = await client.patch(
        f"/api/v1/tenants/{elsewhere}/fields/{made['id']}",
        json={"labels": {"en": "x"}},
        headers=bob,
    )
    assert reached.status_code == 404
    form = await client.post(
        f"/api/v1/tenants/{elsewhere}/forms",
        json={"name": {"en": "x"}, "fields": [{"ref": "custom:locker"}], "credentials": []},
        headers=bob,
    )
    assert form.status_code == 422 and form.json()["detail"]["code"] == "field_unknown"
