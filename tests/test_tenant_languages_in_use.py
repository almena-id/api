"""A tenant writes only in the languages it works in (`Tenant.languages`)."""

from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox, bilingual
from tests.subscriptions import paid_sign_in
from tests.test_directory import _sign_in

SPANISH = {"en": "Request", "es": "Solicitud"}


def _refused(response: Any) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "language_not_enabled"


async def test_a_form_is_written_in_the_tenants_languages(
    client: AsyncClient, outbox: Outbox
) -> None:
    # Ada signed up in English: her tenant works in English alone.
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"
    fields = [{"ref": "given_name"}]
    _refused(await client.post(base, json={"name": SPANISH, "fields": fields}, headers=headers))
    help_in_spanish = [{"ref": "given_name", "help": {"es": "Tu nombre"}}]
    _refused(
        await client.post(
            base, json={"name": {"en": "Request"}, "fields": help_in_spanish}, headers=headers
        )
    )
    purpose = [{"type": "verified_email", "purpose": {"es": "Para escribirte"}}]
    _refused(
        await client.post(
            base, json={"name": {"en": "Request"}, "credentials": purpose}, headers=headers
        )
    )
    made = await client.post(
        base, json={"name": {"en": "Request"}, "fields": fields}, headers=headers
    )
    assert made.status_code == 201

    await bilingual(client, headers, tenant)
    path = f"{base}/{made.json()['id']}"
    changed = await client.put(path, json={"name": SPANISH, "fields": fields}, headers=headers)
    assert changed.status_code == 200, changed.text


async def test_what_was_written_stays_until_written_again(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    await bilingual(client, headers, tenant)
    base = f"/api/v1/tenants/{tenant}/forms"
    fields = [{"ref": "given_name"}]
    made = (
        await client.post(base, json={"name": SPANISH, "fields": fields}, headers=headers)
    ).json()
    # Back to English alone: the Spanish name stays as it was…
    await client.patch(f"/api/v1/tenants/{tenant}", json={"languages": ["en"]}, headers=headers)
    path = f"{base}/{made['id']}"
    assert (await client.get(path, headers=headers)).json()["name"] == SPANISH
    # …until the form is written again, in English.
    _refused(await client.put(path, json={"name": SPANISH, "fields": fields}, headers=headers))
    again = await client.put(
        path, json={"name": {"en": "Request"}, "fields": fields}, headers=headers
    )
    assert again.json()["name"] == {"en": "Request"}


async def test_descriptions_are_in_the_tenants_languages(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/issuers"
    _refused(
        await client.post(
            base, json={"name": "Uni", "description": {"es": "Universidad"}}, headers=headers
        )
    )
    issuer = (await client.post(base, json={"name": "Uni"}, headers=headers)).json()
    _refused(
        await client.patch(
            f"{base}/{issuer['id']}", json={"description": {"es": "Universidad"}}, headers=headers
        )
    )


async def test_own_fields_and_types_are_named_in_the_tenants_languages(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await paid_sign_in(client, outbox, "ada@acme.com")
    fields = f"/api/v1/tenants/{tenant}/fields"
    campus: dict[str, Any] = {
        "key": "campus",
        "type": "code",
        "labels": {"en": "Campus"},
        "options": [
            {"value": "north", "labels": {"en": "North", "es": "Norte"}},
            {"value": "south", "labels": {"en": "South"}},
        ],
    }
    _refused(await client.post(fields, json=campus, headers=headers))
    campus["options"][0]["labels"] = {"en": "North"}
    made = await client.post(fields, json=campus, headers=headers)
    assert made.status_code == 201, made.text
    _refused(
        await client.patch(
            f"{fields}/{made.json()['id']}",
            json={"labels": {"en": "Campus", "es": "Campus"}},
            headers=headers,
        )
    )

    types = f"/api/v1/tenants/{tenant}/credential-types"
    kind = {
        "key": "pass",
        "labels": {"en": "Campus pass", "es": "Pase"},
        "descriptions": {"en": "Who may enter."},
        "category": "membership",
        "claims": [{"field": "given_name"}],
    }
    _refused(await client.post(types, json=kind, headers=headers))
    kind["labels"] = {"en": "Campus pass"}
    made = await client.post(types, json=kind, headers=headers)
    assert made.status_code == 201, made.text
    _refused(
        await client.patch(
            f"{types}/{made.json()['id']}",
            json={"descriptions": {"es": "Quién entra."}},
            headers=headers,
        )
    )
