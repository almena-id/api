import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.mediators import MediatorError
from registry_api.models import Identity
from registry_api.root import RootExists, create_root
from tests.conftest import Outbox
from tests.test_directory import _sign_in


@pytest.fixture(autouse=True)
def did_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "did_url", "https://almena.id")


async def test_the_root_is_created_once(db: AsyncSession) -> None:
    await create_root(db, "Almena", "Root@Almena.id")
    with pytest.raises(RootExists):
        await create_root(db, "Other", "other@almena.id")


async def test_its_invited_admin_joins_it_and_reviews(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root, _, _ = await create_root(db, "Almena", "Root@Almena.id")
    headers, tenant = await _sign_in(client, outbox, "root@almena.id")
    assert tenant == str(root.id)
    assert len((await client.get("/api/v1/tenants", headers=headers)).json()) == 1
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["reviewer"] is True

    other, _ = await _sign_in(client, outbox, "ada@acme.com")
    assert (await client.get("/api/v1/auth/me", headers=other)).json()["reviewer"] is False


async def test_its_identity_is_the_domain_did(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    assert (await client.get("/.well-known/did.json")).status_code == 404

    root, _, _ = await create_root(db, "Almena", "root@almena.id")
    response = await client.get("/.well-known/did.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/did+json"
    assert response.json() == {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": "did:web:almena.id",
    }

    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    identity = await client.get(
        f"/api/v1/tenants/{root.id}/identities/{root.identity_id}", headers=headers
    )
    assert identity.json()["did"] == "did:web:almena.id"
    assert identity.json()["document_url"] == "https://almena.id/.well-known/did.json"

    # One DID only: not also under /ids/.
    slug = (await db.get(Identity, root.identity_id)).slug  # type: ignore[union-attr]
    assert (await client.get(f"/ids/{slug}/did.json")).status_code == 404


async def test_it_comes_with_its_issuer_of_certifications(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root, issuer, _ = await create_root(db, "Almena", "root@almena.id")
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    issuers = (await client.get(f"/api/v1/tenants/{root.id}/issuers", headers=headers)).json()
    assert [item["name"] for item in issuers["items"]] == ["Almena Certification"]

    # An identity of its own, under /ids/, not the root's.
    assert issuer.identity_id != root.identity_id
    document = await client.get(f"/ids/{issuer.identity.slug}/did.json")
    assert document.json()["id"] == f"did:web:almena.id:ids:{issuer.identity.slug}"
    # Controlled by the root.
    assert document.json()["controller"] == "did:web:almena.id"


async def test_it_comes_with_its_mediator(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    root, _, mediator = await create_root(db, "Almena", "root@almena.id")
    assert mediator.url == "https://mediator.almena.id"
    headers, _ = await _sign_in(client, outbox, "root@almena.id")
    listed = (await client.get(f"/api/v1/tenants/{root.id}/mediators", headers=headers)).json()
    assert [(m["name"], m["url"]) for m in listed["items"]] == [
        ("Almena Mediator", "https://mediator.almena.id")
    ]
    # Published at once: its DID resolves and names where it listens.
    document = (await client.get(f"/ids/{mediator.identity.slug}/did.json")).json()
    assert document["service"][0]["serviceEndpoint"]["uri"] == "https://mediator.almena.id"
    assert document["controller"] == "did:web:almena.id"


async def test_its_mediator_url_is_checked(db: AsyncSession) -> None:
    with pytest.raises(MediatorError):
        await create_root(db, "Almena", "root@almena.id", "http://mediator.almena.id")
