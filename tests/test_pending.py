import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Identity, StatusList
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.signing import link_wallet, publish, ready, sign
from tests.test_directory import _sign_in
from tests.test_issuance import _accepted, sign_status_list


def _rows(pending: list[dict[str, Any]]) -> list[tuple[str, str, str, str | None, bool]]:
    return [
        (row["kind"], row["action"], row["state"], row["blocked_by"], row["yours"])
        for row in pending
    ]


async def test_what_waits_comes_in_the_order_it_is_done(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    url = f"{base}/pending"
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()

    # Every DID to sign first, then what is published once they are.
    pending = (await client.get(url, headers=headers)).json()
    assert [row["id"] for row in pending] == [own, issuer["identity"]["id"], issuer["id"]]
    assert _rows(pending) == [
        ("identity", "sign", "pending", None, True),
        ("identity", "sign", "pending", None, True),
        ("issuer", "publish", "draft", "identity_pending", True),
    ]
    assert pending[2]["name"] == "Uni" and pending[2]["issuer"] is None

    # Its own DID signed, the issuer waits for the tenant's.
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    pending = (await client.get(url, headers=headers)).json()
    assert _rows(pending) == [
        ("identity", "sign", "pending", None, True),
        ("issuer", "publish", "draft", "tenant_pending", True),
    ]

    await sign(client, headers, tenant, own, wallet)
    pending = (await client.get(url, headers=headers)).json()
    assert pending[0]["kind"] == "identity" and pending[0]["state"] == "outdated"
    await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    assert _rows((await client.get(url, headers=headers)).json()) == [
        ("issuer", "publish", "draft", None, True),
    ]

    await publish(client, headers, tenant, "issuers", issuer["id"], wallet)
    assert (await client.get(url, headers=headers)).json() == []


async def test_it_says_whose_it_is(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    # Any member reads it; under `any_admin`, signing is the admins'.
    pending = (await client.get(f"/api/v1/tenants/{tenant}/pending", headers=bob)).json()
    assert _rows(pending) == [("identity", "sign", "pending", None, False)]

    other, _ = await _sign_in(client, outbox, "eve@example.org")
    refused = await client.get(f"/api/v1/tenants/{tenant}/pending", headers=other)
    assert refused.status_code == 404


async def test_an_expired_endorsement_is_published_again(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    verifier = (
        await client.post(f"{base}/verifiers", json={"name": "Desk"}, headers=headers)
    ).json()
    await sign(client, headers, tenant, verifier["identity"]["id"], wallet)
    await publish(client, headers, tenant, "verifiers", verifier["id"], wallet)
    assert (await client.get(f"{base}/pending", headers=headers)).json() == []

    await db.execute(
        update(Identity)
        .where(Identity.id == uuid.UUID(verifier["identity"]["id"]))
        .values(endorsed_until=datetime.now(UTC) - timedelta(days=1))
    )
    await db.commit()
    pending = (await client.get(f"{base}/pending", headers=headers)).json()
    assert _rows(pending) == [("verifier", "publish", "expired", None, True)]
    assert pending[0]["id"] == verifier["id"]

    # Publishing again renews it.
    await publish(client, headers, tenant, "verifiers", verifier["id"], wallet)
    assert (await client.get(f"{base}/pending", headers=headers)).json() == []


async def test_credentials_wait_for_their_status_list(client: AsyncClient, outbox: Outbox) -> None:
    club, headers, tenant, application_id, _, _ = await _accepted(client, outbox)
    url = f"/api/v1/tenants/{tenant}/pending"
    issuer = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()
    issuer_id = issuer["items"][0]["id"]

    # No list yet: the credential waits for one, signed by the issuer's signer.
    pending = (await client.get(url, headers=headers)).json()
    assert _rows(pending) == [("credential", "sign", "accepted", "status_list_unsigned", True)]
    assert pending[0]["id"] == application_id
    assert pending[0]["issuer"] == {"id": issuer_id, "name": "Club"}
    assert pending[0]["credential_type"] == "membership"

    await sign_status_list(client, headers, tenant, club.wallet)
    pending = (await client.get(url, headers=headers)).json()
    assert _rows(pending) == [("credential", "sign", "accepted", None, True)]

    # With no signer named, nobody signs the issuer's credentials (and its
    # DID document, which named the signer's key, changes).
    await client.put(
        f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/signing",
        json={"system": None},
        headers=headers,
    )
    pending = (await client.get(url, headers=headers)).json()
    assert _rows(pending) == [
        ("identity", "sign", "outdated", None, True),
        ("credential", "sign", "accepted", "signer_needed", False),
    ]
    assert pending[0]["name"] == "Club"


async def test_status_lists_wait_to_be_signed(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    club, headers, tenant, application_id, _, _ = await _accepted(client, outbox)
    url = f"/api/v1/tenants/{tenant}/pending"
    issuer_id = (await client.get(f"/api/v1/tenants/{tenant}/issuers", headers=headers)).json()[
        "items"
    ][0]["id"]
    lists_url = f"/api/v1/tenants/{tenant}/issuers/{issuer_id}/status-lists"

    # Asked for and not signed: the list waits, before the credential in it.
    asked = await client.post(f"{lists_url}/sign", json={}, headers=headers)
    assert asked.status_code == 200, asked.text
    listed = (await client.get(lists_url, headers=headers)).json()["items"][0]
    pending = (await client.get(url, headers=headers)).json()
    assert _rows(pending) == [
        ("status_list", "sign", "unsigned", None, True),
        ("credential", "sign", "accepted", "status_list_unsigned", True),
    ]
    assert [row["id"] for row in pending] == [listed["id"], application_id]
    assert pending[0]["name"] == listed["slug"]
    assert pending[0]["issuer"] == {"id": issuer_id, "name": "Club"}

    await sign_status_list(client, headers, tenant, club.wallet)
    assert _rows((await client.get(url, headers=headers)).json()) == [
        ("credential", "sign", "accepted", None, True),
    ]

    # Signed with a key its DID no longer lists, it is signed again.
    gone = jwt.encode(
        {},
        "a key no DID lists, and long enough",
        algorithm="HS256",
        headers={"kid": "did:key:x#zGone"},
    )
    await db.execute(
        update(StatusList).where(StatusList.id == uuid.UUID(listed["id"])).values(token=gone)
    )
    await db.commit()
    assert _rows((await client.get(url, headers=headers)).json()) == [
        ("status_list", "sign", "outdated", None, True),
        ("credential", "sign", "accepted", "status_list_unsigned", True),
    ]
