import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.models import Identity
from registry_api.root import RootExists, create_root
from tests.conftest import Outbox
from tests.signing import ready
from tests.test_directory import _sign_in

pytestmark = pytest.mark.no_anchor


@pytest.fixture(autouse=True)
def did_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "did_url", "https://almena.id")


async def test_the_root_is_created_once(db: AsyncSession) -> None:
    await create_root(db, "Almena", "Root@Almena.id")
    with pytest.raises(RootExists):
        await create_root(db, "Other", "other@almena.id")


async def test_its_invited_admin_joins_it(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root = await create_root(db, "Almena", "Root@Almena.id")
    headers, tenant = await _sign_in(client, outbox, "root@almena.id")
    assert tenant == str(root.id)
    assert len((await client.get("/api/v1/tenants", headers=headers)).json()) == 1


async def test_its_identity_is_the_domain_did(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    assert (await client.get("/.well-known/did.json")).status_code == 404

    root = await create_root(db, "Almena", "root@almena.id")
    # Pending until one of its admins signs it from a wallet.
    assert (await client.get("/.well-known/did.json")).status_code == 404
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    wallet = await ready(client, headers, str(root.id))

    identity = (
        await client.get(
            f"/api/v1/tenants/{root.id}/identities/{root.identity_id}", headers=headers
        )
    ).json()
    did = identity["did"]
    assert did.startswith("did:webvh:Qm") and did.endswith(":almena.id")
    assert identity["log_url"] == "https://almena.id/.well-known/did.jsonl"
    assert identity["document_url"] == "https://almena.id/.well-known/did.json"
    log = await client.get("/.well-known/did.jsonl")
    assert log.status_code == 200 and len(log.text.splitlines()) == 1
    response = await client.get("/.well-known/did.json")
    assert response.headers["content-type"] == "application/did+json"
    # Its admins' wallets sign for it: the key, under its did:web name too.
    key = wallet.did.removeprefix("did:key:")
    assert response.json() == {
        "@context": ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/multikey/v1"],
        "id": "did:web:almena.id",
        "verificationMethod": [
            {
                "id": f"did:web:almena.id#{key}",
                "type": "Multikey",
                "controller": "did:web:almena.id",
                "publicKeyMultibase": key,
            }
        ],
        "assertionMethod": [f"did:web:almena.id#{key}"],
        "authentication": [f"did:web:almena.id#{key}"],
        "alsoKnownAs": [did],
    }

    # One DID only: not also under /ids/.
    slug = (await db.get(Identity, root.identity_id)).slug  # type: ignore[union-attr]
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404


async def test_it_comes_with_no_mediator(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root = await create_root(db, "Almena", "root@almena.id")
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    listed = (await client.get(f"/api/v1/tenants/{root.id}/mediators", headers=headers)).json()
    assert listed["items"] == []
    tenant = (await client.get(f"/api/v1/tenants/{root.id}", headers=headers)).json()
    assert tenant["mediator"] is None
