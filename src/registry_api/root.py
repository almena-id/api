"""The root authority: Almena, the tenant that governs the network.

It is an ordinary tenant with no keys of its own on the server: it is created
once at install, by ``registry-api init-root``, and its first admin is invited
by email. Its identity is the identity domain's own DID
(``did:webvh:{SCID}:almena.id``, also served as ``did:web:almena.id``), the
one wallets trust; the keys in its document arrive when its admins sign from
their wallets.

It is created with one mediator, ``Almena Mediator``
(``https://mediator.almena.id`` unless told otherwise), a draft like any other
until an admin publishes it.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import mediators
from registry_api.models import Identity, Mediator, Tenant, TenantInvitation

# The root's mediator, created with it, and where it listens unless told.
ROOT_MEDIATOR = "Almena Mediator"
ROOT_MEDIATOR_URL = "https://mediator.almena.id"


class RootExists(Exception):
    """There is a root already: it is created once."""


async def root_tenant(db: AsyncSession) -> Tenant | None:
    return await db.scalar(select(Tenant).where(Tenant.root))


async def create_root(
    db: AsyncSession, name: str, admin: str, mediator_url: str = ROOT_MEDIATOR_URL
) -> tuple[Tenant, Mediator]:
    """The root tenant, with its identity and its mediator, and `admin`
    invited to run it; they join the next time they sign
    in with that address. `mediator_url` must be one a mediator may have
    (https; `MediatorError` otherwise)."""
    # Imported here: the routes import this module.
    from registry_api.api.routes.auth import new_tenant

    if await root_tenant(db) is not None:
        raise RootExists
    url = mediators.endpoint(mediator_url)
    tenant = await new_tenant(db, name)
    tenant.root = True
    now = datetime.now(UTC)
    relay = Identity(tenant_id=tenant.id, name=ROOT_MEDIATOR, created_at=now)
    db.add(relay)
    await db.flush()
    mediator = Mediator(
        tenant_id=tenant.id,
        name=ROOT_MEDIATOR,
        url=url,
        identity_id=relay.id,
        created_at=now,
    )
    db.add(mediator)
    db.add(TenantInvitation(tenant_id=tenant.id, email=admin.strip().lower(), role="admin"))
    await db.commit()
    await db.refresh(mediator, ["identity"])
    return tenant, mediator
