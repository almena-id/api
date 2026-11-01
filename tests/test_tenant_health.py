import base64
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.models import StatusList, TenantMember
from registry_api.root import create_root
from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import new_mediator, verified_domain
from tests.signing import Headers, link_wallet, publish, ready, sign
from tests.test_directory import _sign_in
from tests.test_issuance import sign_status_list


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
    await publish(client, headers, tenant, "mediators", mediator["id"])
    mediator_id: str = mediator["id"]
    return mediator_id


async def test_a_new_tenant_lacks_a_mediator_and_a_wallet(
    client: AsyncClient, outbox: Outbox
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    health = await _health(client, ada, tenant)
    # Nothing of issuers' or verifiers' is asked while it has none.
    assert _issues(health) == {
        "name": None,
        "mediator": "missing",
        "signing_flow": "no_wallet",
        "published": "missing",
    }
    assert health["score"] == 25


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
    await publish(client, ada, tenant, "mediators", mediator["id"])
    assert _issues(await _health(client, ada, tenant))["published"] == "missing"

    # A verifier, signed, with its signer and its queue, published.
    base = f"/api/v1/tenants/{tenant}/verifiers"
    door = (await client.post(base, json={"name": "Door"}, headers=ada)).json()
    health = await _health(client, ada, tenant)
    assert _issues(health) == {
        "name": None,
        "mediator": None,
        "signing_flow": None,
        "signers": "no_signer",
        "status_lists": None,
        "queues": "missing",
        "published": "unpublished",
    }
    signers = next(c for c in health["checks"] if c["check"] == "signers")
    assert (signers["item"], signers["item_kind"]) == (door["id"], "verifier")
    await _set_signer(client, ada, tenant, "verifiers", door["id"])
    await client.post(f"{base}/{door['id']}/queue", headers=ada)
    await sign(client, ada, tenant, door["identity"]["id"], wallet)
    await publish(client, ada, tenant, "verifiers", door["id"])
    health = await _health(client, ada, tenant)
    assert health["score"] == 100
    assert all(c["done"] for c in health["checks"])


async def _set_signer(
    client: AsyncClient, headers: Headers, tenant: str, kind: str, item_id: str
) -> None:
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    response = await client.put(
        f"/api/v1/tenants/{tenant}/{kind}/{item_id}/signing",
        json={"system": "single_user", "user_id": me["id"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def test_an_issuers_signer_needs_a_wallet_and_its_lists_signed(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    wallet = await ready(client, ada, tenant)
    base = f"/api/v1/tenants/{tenant}/issuers"
    club = (await client.post(base, json={"name": "Club"}, headers=ada)).json()
    await client.put(
        f"{base}/{club['id']}/credential-types", json={"types": ["membership"]}, headers=ada
    )
    # Bob signs for it, but has no wallet.
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    await _sign_in(client, outbox, "bob@example.org")
    members = (await client.get(f"/api/v1/tenants/{tenant}/members", headers=ada)).json()
    bob_id = next(m["user_id"] for m in members if m["email"] == "bob@example.org")
    signing = f"{base}/{club['id']}/signing"
    await client.put(signing, json={"system": "single_user", "user_id": bob_id}, headers=ada)
    assert _issues(await _health(client, ada, tenant))["signers"] == "no_wallet"
    # A draft's lists are asked nothing: its DID does not resolve.
    assert _issues(await _health(client, ada, tenant))["status_lists"] is None

    await _set_signer(client, ada, tenant, "issuers", club["id"])
    await sign(client, ada, tenant, club["identity"]["id"], wallet)
    await publish(client, ada, tenant, "issuers", club["id"])
    health = await _health(client, ada, tenant)
    assert _issues(health)["signers"] is None
    # It grants a type, so it needs a signed list before it issues.
    lists = next(c for c in health["checks"] if c["check"] == "status_lists")
    assert (lists["issue"], lists["item"], lists["item_kind"]) == (
        "unsigned",
        club["id"],
        "issuer",
    )
    await sign_status_list(client, ada, tenant, wallet)
    assert _issues(await _health(client, ada, tenant))["status_lists"] is None

    # Signed by a key its DID no longer lists: stale.
    listed = await db.scalar(
        select(StatusList).where(StatusList.issuer_id == uuid.UUID(club["id"]))
    )
    assert listed is not None and listed.token is not None
    head, _, rest = listed.token.partition(".")
    header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    header["kid"] = header["kid"].rpartition("#")[0] + "#z6MkGone"
    forged = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    listed.token = f"{forged}.{rest}"
    await db.commit()
    assert _issues(await _health(client, ada, tenant))["status_lists"] == "stale"

    # A signer who left signs nothing.
    await client.put(signing, json={"system": "single_user", "user_id": bob_id}, headers=ada)
    await db.delete(await db.get_one(TenantMember, (uuid.UUID(tenant), uuid.UUID(bob_id))))
    await db.commit()
    assert _issues(await _health(client, ada, tenant))["signers"] == "no_signer"


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


@pytest.mark.no_anchor
async def test_new_tenants_start_with_the_roots_mediator(
    client: AsyncClient, outbox: Outbox, db: AsyncSession, dns: Dns
) -> None:
    root = str((await create_root(db, "Almena", "root@almena.id")).id)
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    mediator = await new_mediator(client, headers, root, dns, subdomain="relay", public=True)
    # A draft is nobody's default.
    _, early = await _sign_in(client, outbox, "early@example.org")
    wallet = await ready(client, headers, root)
    await sign(client, headers, root, mediator["identity"]["id"], wallet)
    await publish(client, headers, root, "mediators", mediator["id"])

    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = (await client.get(f"/api/v1/tenants/{tenant}", headers=ada)).json()
    assert body["mediator"] == {"id": mediator["id"], "name": "Relay", "own": False}
    assert _issues(await _health(client, ada, tenant))["mediator"] is None
    early_headers, _ = await _sign_in(client, outbox, "early@example.org")
    assert (await client.get(f"/api/v1/tenants/{early}", headers=early_headers)).json()[
        "mediator"
    ] is None


@pytest.mark.no_anchor
async def test_the_roots_health_is_its_onboarding_in_order(
    client: AsyncClient, outbox: Outbox, db: AsyncSession, dns: Dns
) -> None:
    root = str((await create_root(db, "Almena", "root@almena.id")).id)
    headers, _ = await _sign_in(client, outbox, "root@almena.id")

    async def steps() -> list[tuple[str, str | None]]:
        health = await _health(client, headers, root)
        return [(c["check"], c["issue"]) for c in health["checks"]]

    assert await steps() == [
        ("name", None),
        ("signing_flow", "no_wallet"),
        ("domain", "missing"),
        ("identity", "pending"),
        ("mediator", "missing"),
    ]
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    await verified_domain(client, headers, root, dns)
    own = (await client.get(f"/api/v1/tenants/{root}", headers=headers)).json()
    await sign(client, headers, root, own["identity"]["id"], wallet)
    health = await _health(client, headers, root)
    assert [c["issue"] for c in health["checks"]][:4] == [None] * 4
    assert health["checks"][3]["item"] == own["identity"]["id"]

    mediator = await new_mediator(client, headers, root, dns, subdomain="relay", public=True)
    checks = (await _health(client, headers, root))["checks"]
    assert (checks[4]["issue"], checks[4]["item"]) == ("unsigned", mediator["id"])
    await sign(client, headers, root, mediator["identity"]["id"], wallet)
    assert (await steps())[4] == ("mediator", "unpublished")
    await publish(client, headers, root, "mediators", mediator["id"])
    assert (await steps())[4] == ("mediator", "unchosen")

    picked = {"mediator_id": mediator["id"]}
    await client.patch(f"/api/v1/tenants/{root}", json=picked, headers=headers)
    # Its document now names the mediator's service: signed again.
    assert (await steps())[3:] == [("identity", "outdated"), ("mediator", None)]
    await sign(client, headers, root, own["identity"]["id"], wallet)
    assert (await _health(client, headers, root))["score"] == 100
