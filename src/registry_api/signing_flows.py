"""The signing engine: who signs for a tenant, by the flow the tenant chose.

What a tenant signs as itself — the did:webvh log entries of its identities,
and its endorsements of the issuers, verifiers and mediators it publishes — is
signed by people, from their wallets; the tenant's signing flow says which
people. The registry works out from it the keys that may sign (the log's
`updateKeys`, and the keys its own identity lists), and signs nothing itself.

The catalogue so far:

- ``any_admin``: any one of the tenant's admins with a linked wallet signs,
  alone. The default, and what every tenant starts with.
- ``single_user``: one member of the tenant, named in ``Tenant.signer_id``,
  signs alone — admin or not: signing and managing the account are apart.
  A signer who left the tenant signs nothing.

Changing the flow changes the log's ``updateKeys``, and that entry is signed
by the keys in force: the old flow's signers hand over to the new one's.

Flows that need more than one signature (several admins, named people) will
collect approvals here before anything is published: did:webvh has no
threshold of its own, so the registry holds it.

An issuer's or a verifier's own signing system (``signing.py``) is apart: it
says who signs *as that component*, not as the tenant.
"""

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import webvh
from registry_api.models import Tenant, TenantMember, UserIdentity

Flow = Literal["any_admin", "single_user"]
FLOWS: tuple[Flow, ...] = ("any_admin", "single_user")
DEFAULT: Flow = "any_admin"


async def _admins_wallets(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    subjects = await db.scalars(
        select(UserIdentity.subject)
        .join(TenantMember, TenantMember.user_id == UserIdentity.user_id)
        .where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.role == "admin",
            UserIdentity.provider == "almena",
        )
    )
    return sorted({webvh.multikey(subject) for subject in subjects})


async def _members_wallets(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[str]:
    if await db.get(TenantMember, (tenant_id, user_id)) is None:
        return []
    subjects = await db.scalars(
        select(UserIdentity.subject).where(
            UserIdentity.user_id == user_id, UserIdentity.provider == "almena"
        )
    )
    return sorted({webvh.multikey(subject) for subject in subjects})


async def signs(db: AsyncSession, tenant: Tenant, user_id: uuid.UUID) -> bool:
    """Whether the user signs as the tenant under its flow, wallet or not:
    what the portal offers them (signing, publishing) depends on it."""
    member = await db.get(TenantMember, (tenant.id, user_id))
    if member is None:
        return False
    match tenant.signing_flow:
        case "any_admin":
            return member.role == "admin"
        case "single_user":
            return tenant.signer_id == user_id
        case _:
            return False


async def signers(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """The multikeys that may sign as the tenant, under its flow."""
    tenant = await db.get_one(Tenant, tenant_id)
    match tenant.signing_flow:
        case "any_admin":
            return await _admins_wallets(db, tenant_id)
        case "single_user":
            if tenant.signer_id is None:
                return []
            return await _members_wallets(db, tenant_id, tenant.signer_id)
        case _:
            # A flow this build does not know signs nothing.
            return []
