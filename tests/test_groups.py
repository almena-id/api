from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.subscriptions import ANCHOR, paid_sign_in
from tests.test_directory import _sign_in

PARTS: list[dict[str, Any]] = [
    {
        "key": "name",
        "type": "text",
        "labels": {"en": "Name", "es": "Nombre"},
        "max_length": 100,
    },
    {
        "key": "phone",
        "type": "phone",
        "labels": {"en": "Phone", "es": "Teléfono"},
        "required": False,
    },
    {
        "key": "country",
        "type": "code",
        "labels": {"en": "Country", "es": "País"},
        "domain": "country",
    },
]
CONTACT: dict[str, Any] = {
    "key": "emergency_contact",
    "type": "group",
    "labels": {"en": "Emergency contact", "es": "Contacto de emergencia"},
    "category": "contact",
    "source": "Almena",
    "parts": PARTS,
}


async def _post(
    client: AsyncClient, headers: dict[str, str], url: str, body: dict[str, Any]
) -> Any:
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_anchor_keeps_groups(client: AsyncClient, outbox: Outbox) -> None:
    headers, anchor = await _sign_in(client, outbox, ANCHOR)
    base = f"/api/v1/tenants/{anchor}/fields"
    made = await _post(client, headers, base, CONTACT)
    field = made["field"]
    assert field["type"] == "group"
    assert [(part["key"], part["required"]) for part in field["parts"]] == [
        ("name", True),
        ("phone", False),
        ("country", True),
    ]
    schema = (await client.get("/schemas/fields/v1/emergency_contact.json")).json()
    assert schema["required"] == ["name", "country"]
    assert schema["properties"]["name"]["maxLength"] == 100
    assert len(schema["properties"]["country"]["enum"]) == 249

    for parts, code in (
        ([], "parts_invalid"),
        ([PARTS[0], PARTS[0]], "parts_invalid"),
        ([{**PARTS[0], "labels": {"en": "Name"}}], "parts_invalid"),
        ([{**PARTS[0], "key": "Name"}], "parts_invalid"),
        ([{**PARTS[2], "domain": "nowhere"}], "domain_invalid"),
        ([{**PARTS[0], "options": [{"value": "a", "labels": {"en": "A"}}]}], "constraint_invalid"),
    ):
        response = await client.post(
            base, json={**CONTACT, "key": "other", "parts": parts}, headers=headers
        )
        assert response.status_code == 422, (parts, response.text)
        assert response.json()["detail"] == code, parts

    # Unused, its parts change freely.
    url = f"{base}/{made['id']}"
    reshaped = await client.patch(
        url, json={"type": "group", "parts": [PARTS[0], PARTS[2]]}, headers=headers
    )
    assert reshaped.status_code == 200, reshaped.text
    assert [part["key"] for part in reshaped.json()["field"]["parts"]] == ["name", "country"]

    # In use, it only grows: new parts optional, none taken away nor tightened.
    ada, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    await _post(
        client,
        ada,
        f"/api/v1/tenants/{tenant}/forms",
        {"name": {"en": "x"}, "fields": [{"ref": "emergency_contact"}], "credentials": []},
    )
    longer = {**PARTS[0], "max_length": 200}
    for parts, status in (
        ([longer, PARTS[2], PARTS[1]], 200),
        ([longer, PARTS[2], {**PARTS[1], "key": "email", "type": "email"}], 409),
        ([longer, PARTS[2], {**PARTS[1], "required": True}], 409),
        ([{**PARTS[0], "max_length": 50}, PARTS[2], PARTS[1]], 409),
        ([longer, PARTS[1]], 409),
        ([longer, {**PARTS[2], "type": "codes"}, PARTS[1]], 409),
    ):
        response = await client.patch(url, json={"type": "group", "parts": parts}, headers=headers)
        assert response.status_code == status, (parts, response.text)
    schema = (await client.get("/schemas/fields/v1/emergency_contact.json")).json()
    assert list(schema["properties"]) == ["name", "country", "phone"]
    assert schema["properties"]["name"]["maxLength"] == 200


async def test_groups_are_the_anchors(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    body = {**CONTACT, "category": None, "source": None}
    refused = await client.post(f"/api/v1/tenants/{tenant}/fields", json=body, headers=ada)
    assert refused.status_code == 422 and refused.json()["detail"] == "anchor_only"
    made = await _post(
        client,
        ada,
        f"/api/v1/tenants/{tenant}/fields",
        {"key": "locker", "type": "text", "labels": {"en": "Locker"}},
    )
    turned = await client.patch(
        f"/api/v1/tenants/{tenant}/fields/{made['id']}",
        json={"type": "group", "parts": PARTS},
        headers=ada,
    )
    assert turned.status_code == 422 and turned.json()["detail"] == "anchor_only"
