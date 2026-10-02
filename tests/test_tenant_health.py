import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.models import TenantMember
from registry_api.root import create_root
from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import new_mediator
from tests.signing import Headers, link_wallet, publish, ready, sign
from tests.test_directory import _sign_in


@pytest.fixture(autouse=True)
def did_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "did_url", "https://almena.id")


async def _health(client: AsyncClient, headers: Headers, tenant: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tenants/{tenant}/health", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _issues(health: dict[str, Any]) -> dict[str, str | None]:
    return {c["check"]: c["issue"] for c in health["checks"]}


async def _published_mediator(
    client: AsyncClient,
    headers: Headers,
    tenant: str,
    dns: Dns,
    *,
    public: bool,
    wallet: FakeWallet,
) -> str:
    mediator = await new_mediator(client, headers, tenant, dns, subdomain="relay", public=public)
    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    await publish(client, headers, tenant, "mediators", mediator["id"], wallet)
    mediator_id: str = mediator["id"]
    return mediator_id


async def test_a_new_tenant_lacks_a_mediator_and_a_wallet(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    health = await _health(client, ada, tenant)
    assert _issues(health) == {"name": None, "mediator": "missing", "signing_flow": "no_wallet"}
    assert health["score"] == 33


async def test_it_is_whole_once_set_up(client: AsyncClient, outbox: Outbox, dns: Dns) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    wallet = await ready(client, ada, tenant)
    assert _issues(await _health(client, ada, tenant))["signing_flow"] is None

    mediator = await new_mediator(client, ada, tenant, dns, subdomain="relay")
    picked = {"mediator_id": mediator["id"]}
    await client.patch(f"/api/v1/tenants/{tenant}", json=picked, headers=ada)
    # A draft's DID does not resolve: nothing routes through it yet.
    assert _issues(await _health(client, ada, tenant))["mediator"] == "unpublished"

    await sign(client, ada, tenant, mediator["identity"]["id"], wallet)
    await publish(client, ada, tenant, "mediators", mediator["id"], wallet)
    health = await _health(client, ada, tenant)
    assert health["score"] == 100
    assert all(c["done"] for c in health["checks"])


async def test_a_signer_who_left_signs_nothing(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    await link_wallet(client, bob, FakeWallet())
    members = (await client.get(f"/api/v1/tenants/{tenant}/members", headers=ada)).json()
    bob_id = next(m["user_id"] for m in members if m["email"] == "bob@example.org")
    await client.patch(
        f"/api/v1/tenants/{tenant}",
        json={"signing_flow": "single_user", "signer_id": bob_id},
        headers=ada,
    )
    assert _issues(await _health(client, ada, tenant))["signing_flow"] is None

    await db.delete(await db.get_one(TenantMember, (uuid.UUID(tenant), uuid.UUID(bob_id))))
    await db.commit()
    assert _issues(await _health(client, ada, tenant))["signing_flow"] == "no_signer"


async def test_members_see_it_outsiders_do_not(client: AsyncClient, outbox: Outbox) -> None:
    _, tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    response = await client.get(f"/api/v1/tenants/{tenant}/health", headers=bob)
    assert response.status_code == 404


async def test_a_public_mediator_is_offered_to_every_tenant(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    ada, adas = await _sign_in(client, outbox, "ada@example.org")
    bob, bobs = await _sign_in(client, outbox, "bob@example.org")
    wallet = await ready(client, ada, adas)
    private = await _published_mediator(client, ada, adas, dns, public=False, wallet=wallet)
    public = await _published_mediator(client, ada, adas, dns, public=True, wallet=wallet)
    draft = (
        await new_mediator(client, ada, adas, dns, name="Draft", subdomain="draft", public=True)
    )["id"]

    choices = (await client.get(f"/api/v1/tenants/{bobs}/mediator-choices", headers=bob)).json()
    assert [(c["id"], c["own"], c["published"]) for c in choices] == [(public, False, True)]
    own = (await client.get(f"/api/v1/tenants/{adas}/mediator-choices", headers=ada)).json()
    assert {c["id"] for c in own} == {private, public, draft}

    for refused in (private, draft):
        response = await client.patch(
            f"/api/v1/tenants/{bobs}", json={"mediator_id": refused}, headers=bob
        )
        assert response.json()["detail"] == "mediator_not_found"
    url = f"/api/v1/tenants/{bobs}"
    picked = await client.patch(url, json={"mediator_id": public}, headers=bob)
    assert picked.json()["mediator"] == {"id": public, "name": "Relay", "own": False}
    issuer = await client.post(
        f"/api/v1/tenants/{bobs}/issuers", json={"name": "Bob", "mediator_id": public}, headers=bob
    )
    assert issuer.status_code == 201 and issuer.json()["mediator"]["own"] is False

    # No longer offered once private; those who picked it keep it.
    await client.patch(
        f"/api/v1/tenants/{adas}/mediators/{public}", json={"public": False}, headers=ada
    )
    assert (await client.get(f"/api/v1/tenants/{bobs}/mediator-choices", headers=bob)).json() == []
    kept = (await client.get(f"/api/v1/tenants/{bobs}", headers=bob)).json()
    assert kept["mediator"]["id"] == public


async def test_new_tenants_start_with_the_roots_mediator(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root, mediator = await create_root(db, "Almena", "root@almena.id")
    assert mediator.public
    # A draft is nobody's default.
    _, early = await _sign_in(client, outbox, "early@example.org")
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    wallet = await ready(client, headers, str(root.id))
    await sign(client, headers, str(root.id), str(mediator.identity_id), wallet)
    await publish(client, headers, str(root.id), "mediators", str(mediator.id), wallet)

    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = (await client.get(f"/api/v1/tenants/{tenant}", headers=ada)).json()
    assert body["mediator"] == {"id": str(mediator.id), "name": "Almena Mediator", "own": False}
    assert _issues(await _health(client, ada, tenant))["mediator"] is None
    early_headers, _ = await _sign_in(client, outbox, "early@example.org")
    assert (await client.get(f"/api/v1/tenants/{early}", headers=early_headers)).json()[
        "mediator"
    ] is None
