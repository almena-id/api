"""The root authority: Almena, the tenant the others are certified by.

It is an ordinary tenant with no keys of its own on the server: it is created
once at install, by ``registry-api init-root``, and its first admin is invited
by email. Its members are Almena's reviewers, and its identity is the identity
domain's own DID (``did:web:almena.id``), the one wallets trust; the keys in its
document arrive when its members sign from their wallets.

It is created with one issuer, ``Almena Certification``, with an identity of its
own like any issuer: the one that issues the other tenants' certifications,
published at once; and one mediator, ``Almena Mediator``
(``https://mediator.almena.id`` unless told otherwise), published too.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import mediators
from registry_api.models import Identity, Issuer, Mediator, Tenant, TenantInvitation, TenantMember

# The root's issuer of certifications, created with it.
CERTIFICATION_ISSUER = "Almena Certification"
# The root's mediator, created with it, and where it listens unless told.
ROOT_MEDIATOR = "Almena Mediator"
ROOT_MEDIATOR_URL = "https://mediator.almena.id"


class RootExists(Exception):
    """There is a root already: it is created once."""


async def root_tenant(db: AsyncSession) -> Tenant | None:
    return await db.scalar(select(Tenant).where(Tenant.root))


async def is_reviewer(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the user belongs to the root, and so reviews for Almena."""
    member = await db.scalar(
        select(TenantMember.user_id)
        .join(Tenant, Tenant.id == TenantMember.tenant_id)
        .where(Tenant.root, TenantMember.user_id == user_id)
    )
    return member is not None


async def create_root(
    db: AsyncSession, name: str, admin: str, mediator_url: str = ROOT_MEDIATOR_URL
) -> tuple[Tenant, Issuer, Mediator]:
    """The root tenant, with its identity, its issuer of certifications and its
    mediator, and `admin` invited to run it; they join the next time they sign
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
    identity = Identity(tenant_id=tenant.id, name=CERTIFICATION_ISSUER, created_at=now)
    db.add(identity)
    await db.flush()
    issuer = Issuer(
        tenant_id=tenant.id,
        name=CERTIFICATION_ISSUER,
        description="Certifies the tenants of the network.",
        identity_id=identity.id,
        created_at=now,
        # The reference others check certifications against: public at once.
        published_at=now,
    )
    db.add(issuer)
    relay = Identity(tenant_id=tenant.id, name=ROOT_MEDIATOR, created_at=now)
    db.add(relay)
    await db.flush()
    mediator = Mediator(
        tenant_id=tenant.id,
        name=ROOT_MEDIATOR,
        url=url,
        identity_id=relay.id,
        created_at=now,
        published_at=now,
    )
    db.add(mediator)
    db.add(TenantInvitation(tenant_id=tenant.id, email=admin.strip().lower(), role="admin"))
    await db.commit()
    await db.refresh(issuer, ["identity"])
    await db.refresh(mediator, ["identity"])
    return tenant, issuer, mediator
