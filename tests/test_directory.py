import pytest
from httpx import AsyncClient

from tests.conftest import Outbox


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> tuple[dict[str, str], str]:
    """Headers for a new account, and the id of the tenant it was created with."""
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    tenant = (await client.get("/api/v1/tenants", headers=headers)).json()[0]["id"]
    return headers, tenant


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_create_then_list(client: AsyncClient, outbox: Outbox, kind: str) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/{kind}"

    empty = (await client.get(base, headers=headers)).json()
    assert empty == {"items": [], "next_cursor": None, "total": 0}

    body = {"name": "  Acme  ", "description": "  "}
    created = await client.post(base, json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Acme"
    assert created.json()["description"] is None

    listed = (await client.get(base, headers=headers)).json()
    assert [i["name"] for i in listed["items"]] == ["Acme"]
    assert listed["total"] == 1


async def test_descriptions_are_kept(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = {"name": "Uni", "description": "Degrees"}
    created = await client.post(f"/api/v1/tenants/{tenant}/issuers", json=body, headers=headers)
    assert created.json()["description"] == "Degrees"


async def test_a_name_is_required(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    for name in ["", "   ", "x" * 201]:
        response = await client.post(
            f"/api/v1/tenants/{tenant}/verifiers", json={"name": name}, headers=headers
        )
        assert response.status_code == 422


async def test_pages_follow_the_cursor_to_the_end(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/verifiers"
    for n in range(7):
        await client.post(base, json={"name": f"id-{n}"}, headers=headers)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while pages < 10:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(base, params=params, headers=headers)).json()
        assert page["total"] == 7
        seen += [i["name"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert sorted(seen) == [f"id-{n}" for n in range(7)]
    assert len(set(seen)) == 7


async def test_a_bad_cursor_is_refused(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    response = await client.get(
        f"/api/v1/tenants/{tenant}/issuers", params={"cursor": "nonsense"}, headers=headers
    )
    assert response.status_code == 400


async def test_another_tenant_is_out_of_reach(client: AsyncClient, outbox: Outbox) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    base = f"/api/v1/tenants/{ada_tenant}/issuers"
    await client.post(base, json={"name": "Ada's"}, headers=ada)

    assert (await client.get(base, headers=bob)).status_code == 404
    assert (await client.post(base, json={"name": "Bob's"}, headers=bob)).status_code == 404
    assert (await client.get(base)).status_code == 401


async def test_an_issuer_gets_an_identity_of_its_own(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    assert issuer["identity"]["name"] == "Uni"

    identities = (await client.get(f"{base}/identities", headers=headers)).json()
    # The tenant's own, and the issuer's (newest first).
    assert identities["total"] == 2
    newest = identities["items"][0]
    assert newest["id"] == issuer["identity"]["id"]
    assert newest["used_by"] == [{"kind": "issuer", "id": issuer["id"], "name": "Uni"}]


async def test_one_identity_can_issue_and_verify(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    acme = (await client.post(f"{base}/identities", json={"name": "Acme"}, headers=headers)).json()
    assert acme["used_by"] == []
    body = {"name": "Acme desk", "identity_id": acme["id"]}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    verifier = (await client.post(f"{base}/verifiers", json=body, headers=headers)).json()
    assert issuer["identity"]["id"] == verifier["identity"]["id"] == acme["id"]

    identities = (await client.get(f"{base}/identities", headers=headers)).json()
    assert identities["total"] == 2
    kinds = sorted(u["kind"] for u in identities["items"][0]["used_by"])
    assert kinds == ["issuer", "verifier"]

    verifiers = (await client.get(f"{base}/verifiers", headers=headers)).json()
    assert verifiers["items"][0]["identity"] == {"id": acme["id"], "name": "Acme"}


async def test_another_tenants_identity_is_refused(client: AsyncClient, outbox: Outbox) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, bob_tenant = await _sign_in(client, outbox, "bob@example.org")
    bobs = await client.post(
        f"/api/v1/tenants/{bob_tenant}/identities", json={"name": "Bob"}, headers=bob
    )
    response = await client.post(
        f"/api/v1/tenants/{ada_tenant}/issuers",
        json={"name": "Sneaky", "identity_id": bobs.json()["id"]},
        headers=ada,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "identity_not_found"


async def test_the_tenant_has_an_identity_of_its_own(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    identities = (await client.get(f"{base}/identities", headers=headers)).json()
    assert identities["total"] == 1
    own = identities["items"][0]
    assert own["name"] == "Tenant of ada@example.org"
    assert own["used_by"] == [{"kind": "tenant", "id": tenant, "name": own["name"]}]

    detail = (await client.get(base, headers=headers)).json()
    assert detail["identity"] == {"id": own["id"], "name": own["name"]}

    # Renaming the tenant renames its identity with it.
    await client.patch(base, json={"name": "Acme"}, headers=headers)
    renamed = (await client.get(f"{base}/identities", headers=headers)).json()["items"][0]
    assert renamed["name"] == "Acme"
