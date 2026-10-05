from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient

from registry_api import entitlements
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.subscriptions import paid_sign_in, subscribe
from tests.test_directory import _sign_in
from tests.test_presentations import AUD, NONCE, _issuer, sd_jwt

ADA = "ada@acme.com"
CAMPUS: dict[str, Any] = {
    "key": "campus",
    "type": "code",
    "labels": {"en": "Campus"},
    "options": [
        {"value": "north", "labels": {"en": "North"}},
        {"value": "south", "labels": {"en": "South"}},
    ],
}
PASS: dict[str, Any] = {
    "key": "pass",
    "labels": {"en": "Campus pass"},
    "descriptions": {"en": "Who may enter which campus."},
    "category": "membership",
    "claims": [{"field": "given_name"}, {"field": "custom:campus"}],
    "w3c_type": "CampusPassCredential",
}


async def _post(
    client: AsyncClient, headers: dict[str, str], url: str, body: dict[str, Any]
) -> Any:
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _own_type(
    client: AsyncClient, outbox: Outbox, email: str
) -> tuple[dict[str, str], str, Any]:
    """A subscribed tenant with a field and a credential type of its own."""
    headers, tenant = await paid_sign_in(client, outbox, email)
    await _post(client, headers, f"/api/v1/tenants/{tenant}/fields", CAMPUS)
    made = await _post(client, headers, f"/api/v1/tenants/{tenant}/credential-types", PASS)
    return headers, tenant, made


async def test_a_tenant_keeps_types_of_its_own(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant, made = await _own_type(client, outbox, ADA)
    assert made["ref"] == "custom:pass" and made["key"] == "pass"
    kind = made["type"]
    assert kind["id"] == "custom:pass" and kind["source"] == ""
    assert kind["claims"][1] == {"field": "custom:campus", "name": "campus", "required": True}
    vct = kind["formats"]["dc+sd-jwt"]["vct"]
    slug = vct.removeprefix("https://almena.id/credentials/").split("/")[0]
    assert slug.startswith("ten_") and vct.endswith("/pass/v1")
    listed = await client.get(f"/api/v1/tenants/{tenant}/credential-types", headers=headers)
    assert [item["ref"] for item in listed.json()] == ["custom:pass"]

    # Published under the tenant's name, as its credentials leave the platform.
    metadata = (await client.get(f"/.well-known/vct/credentials/{slug}/pass/v1")).json()
    assert metadata["vct"] == vct and metadata["name"] == "Campus pass"
    assert [claim["path"] for claim in metadata["claims"]] == [["given_name"], ["campus"]]
    assert [entry["locale"] for entry in metadata["display"]] == ["en"]
    schema = (await client.get(f"/schemas/credentials/v1/{slug}/pass.json")).json()
    assert schema["properties"]["given_name"] == {
        "$ref": "https://almena.id/schemas/fields/v1/given_name.json"
    }
    assert schema["properties"]["campus"]["enum"] == ["north", "south"]
    assert schema["required"] == ["given_name", "campus"]
    assert (await client.get("/.well-known/vct/credentials/ten_nobody/pass/v1")).status_code == 404

    # Not Almena's catalogue: no other tenant sees it.
    public = (await client.get("/api/v1/catalog/credentials")).json()["types"]
    assert "custom:pass" not in [item["id"] for item in public]
    other, elsewhere = await _sign_in(client, outbox, "bob@other.org")
    form = await client.post(
        f"/api/v1/tenants/{elsewhere}/forms",
        json={"name": {"en": "x"}, "fields": [], "credentials": [{"type": "custom:pass"}]},
        headers=other,
    )
    assert form.status_code == 422 and form.json()["detail"]["code"] == "credential_unknown"
    issuer = await _post(
        client,
        other,
        f"/api/v1/tenants/{elsewhere}/issuers",
        {"name": "B", "description": {"en": "x"}},
    )
    granted = await client.put(
        f"/api/v1/tenants/{elsewhere}/issuers/{issuer['id']}/credential-types",
        json={"types": ["custom:pass"]},
        headers=other,
    )
    assert granted.status_code == 422


async def test_its_types_are_checked(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant, _ = await _own_type(client, outbox, ADA)
    other, elsewhere = await paid_sign_in(client, outbox, "bob@other.org")
    await _post(
        client,
        other,
        f"/api/v1/tenants/{elsewhere}/fields",
        {"key": "locker", "type": "text", "labels": {"en": "Locker"}},
    )
    base = f"/api/v1/tenants/{tenant}/credential-types"
    for change, code in (
        ({"key": "pass2", "labels": {}}, "labels_required"),
        ({"key": "pass2", "descriptions": {"fr": "x"}}, "descriptions_required"),
        ({"key": "pass2", "issuance": "external", "vct": "urn:x:1"}, "issuance_invalid"),
        ({"key": "pass2", "claims": [{"field": "document_file"}]}, "claims_invalid"),
        ({"key": "pass2", "claims": [{"field": "custom:locker"}]}, "claims_invalid"),
        ({"key": "pass2", "w3c_type": "MembershipCredential"}, "w3c_type_invalid"),
    ):
        response = await client.post(base, json={**PASS, **change}, headers=headers)
        assert response.status_code == 422, (change, response.text)
        assert response.json()["detail"] == code, change
    again = await client.post(base, json=PASS, headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "key_exists"
    # Named like one of Almena's: its own is `custom:membership`.
    same = await _post(
        client, headers, base, {**PASS, "key": "membership", "w3c_type": "OwnMembership"}
    )
    assert same["ref"] == "custom:membership"


async def test_its_issuers_and_forms_use_them(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant, made = await _own_type(client, outbox, ADA)
    issuer = await _post(
        client,
        headers,
        f"/api/v1/tenants/{tenant}/issuers",
        {"name": "Uni", "description": {"en": "x"}},
    )
    granted = await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer['id']}/credential-types",
        json={"types": ["custom:pass", "membership"]},
        headers=headers,
    )
    # Almena's first, then its own: the catalogue's order.
    assert granted.json()["types"] == ["membership", "custom:pass"]

    form = await _post(
        client,
        headers,
        f"/api/v1/tenants/{tenant}/forms",
        {
            "name": {"en": "Entry"},
            "fields": [{"ref": "given_name"}, {"ref": "custom:campus"}],
            "credentials": [{"type": "custom:pass"}],
        },
    )
    entry = form["credentials"][0]
    assert entry["key"] == "pass" and entry["claims"] == ["given_name", "campus"]
    assert entry["fills"] == ["given_name", "campus"]
    dcql = (
        await client.get(f"/api/v1/tenants/{tenant}/forms/{form['id']}/dcql", headers=headers)
    ).json()
    assert dcql["credentials"][0]["meta"]["vct_values"] == [
        made["type"]["formats"]["dc+sd-jwt"]["vct"]
    ]
    assert dcql["credentials"][0]["claims"] == [{"path": ["given_name"]}, {"path": ["campus"]}]

    # In use, it stays; and so does the field it carries.
    url = f"/api/v1/tenants/{tenant}/credential-types/{made['id']}"
    gone = await client.delete(url, headers=headers)
    assert gone.status_code == 409 and gone.json()["detail"] == "credential_type_in_use"
    narrowed = await client.patch(url, json={"claims": [{"field": "given_name"}]}, headers=headers)
    assert narrowed.status_code == 409
    fields = (await client.get(f"/api/v1/tenants/{tenant}/fields", headers=headers)).json()
    campus = f"/api/v1/tenants/{tenant}/fields/{fields[0]['id']}"
    kept = await client.delete(campus, headers=headers)
    assert kept.status_code == 409 and kept.json()["detail"] == "field_in_use"


async def test_only_its_issuers_are_trusted(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant, made = await _own_type(client, outbox, ADA)
    vct = made["type"]["formats"]["dc+sd-jwt"]["vct"]
    uni = await _issuer(client, outbox, ADA, ["custom:pass"])
    # Another tenant with a type of the same name, its issuer granting it.
    await _own_type(client, outbox, "bob@other.org")
    rival = await _issuer(client, outbox, "bob@other.org", ["custom:pass"])

    # The public catalogue finds who grants it only in its tenant.
    search = "/api/v1/catalog/issuers?grants=custom:pass"
    unnamed = await client.get(search)
    assert unnamed.status_code == 422 and unnamed.json()["detail"] == "tenant_required"
    did = (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["identity"]["did"]
    found = (await client.get(search, params={"tenant": did})).json()["items"]
    assert [item["did"] for item in found] == [uni.did]

    # Trusting named issuers: only its own may be named.
    own = {"type": "custom:pass", "trust": "issuers"}
    rival_named = await client.post(
        f"/api/v1/tenants/{tenant}/forms",
        json={"name": {"en": "x"}, "fields": [], "credentials": [{**own, "issuers": [rival.did]}]},
        headers=headers,
    )
    assert rival_named.json()["detail"]["code"] == "credential_trust_invalid"

    form = await _post(
        client,
        headers,
        f"/api/v1/tenants/{tenant}/forms",
        {
            "name": {"en": "Entry"},
            "fields": [{"ref": "given_name"}, {"ref": "custom:campus"}],
            "credentials": [{"type": "custom:pass"}],
        },
    )
    # Offered, holders see it by name in the public catalogue.
    issuers = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    uni_id = next(item["id"] for item in issuers["items"] if item["identity"]["did"] == uni.did)
    offered = await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{uni_id}/credential-types",
        json={"types": ["custom:pass"], "forms": {"custom:pass": form["id"]}},
        headers=headers,
    )
    assert offered.status_code == 200, offered.text
    offer = await client.get(f"/api/v1/catalog/issuers/{found[0]['slug']}/offers/custom:pass")
    assert offer.json()["credential_type"]["labels"] == {"en": "Campus pass"}
    assert offer.json()["form"]["credentials"][0]["labels"] == {"en": "Campus pass"}

    url = f"/api/v1/tenants/{tenant}/forms/{form['id']}/verify"
    holder = FakeWallet()
    claims = {"given_name": "Lucía", "campus": "north"}
    for issuer, verified, problems in ((uni, True, []), (rival, False, ["issuer_untrusted"])):
        response = await client.post(
            url,
            json={
                "vp_token": {"pass_sd_jwt": [sd_jwt(issuer, holder, claims, vct=vct)]},
                "nonce": NONCE,
                "audience": AUD,
            },
            headers=headers,
        )
        result = response.json()
        assert result["verified"] is verified, result
        assert result["credentials"][0]["problems"] == problems
        if verified:
            assert result["credentials"][0]["fills"] == claims


async def test_a_subscription_is_needed(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, ADA)
    fields = f"/api/v1/tenants/{tenant}/fields"
    types = f"/api/v1/tenants/{tenant}/credential-types"

    # Free: nothing of its own.
    assert (await client.get("/api/v1/tenants", headers=headers)).json()[0]["features"] == []
    mine = (await client.get(f"/api/v1/tenants/{tenant}/subscription", headers=headers)).json()
    assert mine == {
        "plan": None,
        "status": None,
        "current_period_end": None,
        "in_force": False,
        "features": [],
        "updated_at": None,
    }
    for url, body in ((fields, CAMPUS), (types, PASS)):
        refused = await client.post(url, json=body, headers=headers)
        assert refused.status_code == 403 and refused.json()["detail"] == "subscription_required"
    assert (await client.get(types, headers=headers)).status_code == 200

    # Subscribed, it makes them.
    await subscribe(client, outbox, tenant)
    mine = (await client.get(f"/api/v1/tenants/{tenant}/subscription", headers=headers)).json()
    assert mine["in_force"] and mine["features"] == ["own_fields", "own_credential_types"]
    campus = await _post(client, headers, fields, CAMPUS)
    made = await _post(client, headers, types, PASS)

    # A payment missed: still in use for the grace days past its end.
    soon = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await subscribe(client, outbox, tenant, status="past_due", current_period_end=soon)
    assert (await client.get("/api/v1/tenants", headers=headers)).json()[0]["features"]
    lapsed = (datetime.now(UTC) - entitlements.GRACE - timedelta(days=1)).isoformat()
    await subscribe(client, outbox, tenant, status="past_due", current_period_end=lapsed)
    assert (await client.get("/api/v1/tenants", headers=headers)).json()[0]["features"] == []

    # Ended: what it made stays and works, it is not changed again; what nothing
    # uses may still go.
    await subscribe(client, outbox, tenant, status="canceled")
    assert [item["ref"] for item in (await client.get(types, headers=headers)).json()] == [
        "custom:pass"
    ]
    form = await client.post(
        f"/api/v1/tenants/{tenant}/forms",
        json={"name": {"en": "x"}, "fields": [{"ref": "custom:campus"}], "credentials": []},
        headers=headers,
    )
    assert form.status_code == 201
    for url in (f"{fields}/{campus['id']}", f"{types}/{made['id']}"):
        changed = await client.patch(url, json={"labels": {"en": "x"}}, headers=headers)
        assert changed.status_code == 403
    assert (await client.delete(f"{types}/{made['id']}", headers=headers)).status_code == 204
    used = await client.delete(f"{fields}/{campus['id']}", headers=headers)
    assert used.status_code == 409


async def test_the_anchor_manages_subscriptions(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, ADA)
    anchor, root = await _sign_in(client, outbox, "anchor@example.net")
    accounts = f"/api/v1/tenants/{root}/accounts"

    listed = (await client.get(accounts, headers=anchor)).json()
    assert [item["id"] for item in listed["items"]] == [tenant]
    assert listed["items"][0]["subscription"]["status"] is None
    assert listed["items"][0]["members"] == 1
    url = f"{accounts}/{tenant}/subscription"
    for body, code in (
        ({"plan": "gold", "status": "active"}, "plan_invalid"),
        (
            {"plan": "standard", "status": "active", "current_period_end": "2020-01-01T00:00:00Z"},
            "period_end_invalid",
        ),
    ):
        refused = await client.put(url, json=body, headers=anchor)
        assert refused.status_code == 422 and refused.json()["detail"] == code
    until = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    granted = await client.put(
        url,
        json={
            "plan": "standard",
            "status": "active",
            "current_period_end": until.isoformat(),
            "note": "Invoice 2026-001",
        },
        headers=anchor,
    )
    assert granted.status_code == 200, granted.text
    account = granted.json()
    assert account["note"] == "Invoice 2026-001" and account["subscription"]["in_force"]
    assert (await client.get(f"{accounts}?subscribed=yes", headers=anchor)).json()["items"]
    assert not (await client.get(f"{accounts}?subscribed=no", headers=anchor)).json()["items"]
    assert (await client.get(f"{accounts}?q=ADA", headers=anchor)).json()["items"]

    # The account sees it, not the note.
    mine = (await client.get(f"/api/v1/tenants/{tenant}/subscription", headers=ada)).json()
    assert mine["status"] == "active" and "note" not in mine

    removed = await client.delete(url, headers=anchor)
    assert removed.status_code == 204
    assert (await client.get(f"/api/v1/tenants/{tenant}/subscription", headers=ada)).json()[
        "status"
    ] is None

    # Only the anchor's admins; the anchor itself is not an account.
    refused = await client.get(f"/api/v1/tenants/{tenant}/accounts", headers=ada)
    assert refused.status_code == 403 and refused.json()["detail"] == "anchor_only"
    itself = await client.get(f"{accounts}/{root}", headers=anchor)
    assert itself.status_code == 404 and itself.json()["detail"] == "account_not_found"
