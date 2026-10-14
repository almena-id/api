"""A tenant's health: the least it needs set up to operate, as a list of checks.

Each check is done or not, and when not, says what is missing (`issue`), so
the portal can point at the task. The score is the share of checks done. The
catalogue so far:

- ``name``: the tenant has a name (one signed up from a wallet starts with none).
- ``mediator``: it receives messages through a mediator, and that mediator is
  published (a draft's DID does not resolve: nothing routes through it).
- ``signing_flow``: somebody can sign as it under its flow, with a wallet:
  under ``single_user``, a signer who still belongs; under either, a linked
  wallet.

The root has no other tenant's mediator to receive through, so its list is
its onboarding, in the order it can be done — each step needs the ones
before it:

- ``name``;
- ``signing_flow``: a linked wallet, whose key signs everything after;
- ``domain``: a verified domain, the address its mediator is served under
  (and named in its DID document, so verified before signing saves a
  signature);
- ``identity``: its own DID signed; ``outdated`` once its document changes —
  above all when it chooses its mediator, whose service it then names;
- ``mediator``: its own mediator created (``missing``), its DID signed after
  the tenant's, so that it names the tenant as controller (``unsigned``),
  published (``unpublished``), and chosen as the tenant's (``unchosen``).

New checks are added here, in the order the portal lists them.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import dids, signing_flows
from registry_api.config import get_settings
from registry_api.models import Identity, Mediator, Tenant, TenantDomain, TenantMember

Check = Literal["name", "signing_flow", "domain", "identity", "mediator"]
Issue = Literal[
    "missing",
    "unverified",
    "pending",
    "outdated",
    "unsigned",
    "unpublished",
    "unchosen",
    "no_signer",
    "no_wallet",
]


@dataclass(frozen=True)
class Result:
    check: Check
    # `None` when done.
    issue: Issue | None
    # What the task is done on, when it is one thing: the tenant's identity,
    # or the mediator to sign, publish or choose.
    item: uuid.UUID | None = None


async def _mediator(db: AsyncSession, tenant: Tenant) -> Issue | None:
    mediator = await db.get(Mediator, tenant.mediator_id) if tenant.mediator_id else None
    if mediator is None:
        return "missing"
    return "unpublished" if mediator.published_at is None else None


async def _signing_flow(db: AsyncSession, tenant: Tenant) -> Issue | None:
    if tenant.signing_flow == "single_user" and (
        tenant.signer_id is None
        or await db.get(TenantMember, (tenant.id, tenant.signer_id)) is None
    ):
        return "no_signer"
    return None if await signing_flows.signers(db, tenant.id) else "no_wallet"


async def _domain(db: AsyncSession, tenant: Tenant) -> Issue | None:
    verified = list(
        await db.scalars(
            select(TenantDomain.verified_at).where(TenantDomain.tenant_id == tenant.id)
        )
    )
    if not verified:
        return "missing"
    return None if any(at is not None for at in verified) else "unverified"


async def _identity(db: AsyncSession, tenant: Tenant) -> Result:
    identity = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    if identity is None:
        return Result("identity", "pending")
    state = await dids.status_of(db, get_settings().did_url, identity)
    issue: Issue | None = None if state == "signed" else state
    return Result("identity", issue, identity.id)


async def _own_mediator(db: AsyncSession, tenant: Tenant) -> Result:
    chosen = await db.get(Mediator, tenant.mediator_id) if tenant.mediator_id else None
    if chosen is not None and chosen.tenant_id != tenant.id:
        return Result("mediator", await _mediator(db, tenant), chosen.id)
    # The one chosen; else its own, the furthest along first.
    mediator = chosen or await db.scalar(
        select(Mediator)
        .where(Mediator.tenant_id == tenant.id)
        .order_by(Mediator.published_at.is_(None), Mediator.created_at, Mediator.id)
        .limit(1)
    )
    if mediator is None:
        return Result("mediator", "missing")
    if mediator.published_at is None:
        identity = await db.get_one(Identity, mediator.identity_id)
        signed = await dids.status_of(db, get_settings().did_url, identity) == "signed"
        return Result("mediator", "unpublished" if signed else "unsigned", mediator.id)
    return Result("mediator", None if chosen else "unchosen", mediator.id)


async def checks(db: AsyncSession, tenant: Tenant) -> list[Result]:
    if tenant.root:
        return [
            Result("name", None if (tenant.name or "").strip() else "missing"),
            Result("signing_flow", await _signing_flow(db, tenant)),
            Result("domain", await _domain(db, tenant)),
            await _identity(db, tenant),
            await _own_mediator(db, tenant),
        ]
    return [
        Result("name", None if (tenant.name or "").strip() else "missing"),
        Result("mediator", await _mediator(db, tenant)),
        Result("signing_flow", await _signing_flow(db, tenant)),
    ]


def score(results: list[Result]) -> int:
    """The share of checks done, as a whole percentage."""
    done = sum(1 for result in results if result.issue is None)
    return round(100 * done / len(results)) if results else 100
