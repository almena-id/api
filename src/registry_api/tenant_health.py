"""A tenant's health: the least it needs set up to operate, as a list of checks.

Each check is done or not, and when not, says what is missing (`issue`), so
the portal can point at the task. The score is the share of checks done. The
catalogue so far:

- ``name``: the tenant has a name (one signed up from a wallet starts with none).
- ``mediator``: it receives messages through a mediator, and that mediator is
  published (a draft's DID does not resolve: nothing routes through it).
- ``signing_flow``: somebody can sign as it under its flow, with a wallet:
  under ``single_user``, a signer who still belongs; under either, a linked wallet.

New checks are added here, in the order the portal lists them.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import signing_flows
from registry_api.models import Mediator, Tenant, TenantMember

Check = Literal["name", "mediator", "signing_flow"]
Issue = Literal["missing", "unpublished", "no_signer", "no_wallet"]


@dataclass(frozen=True)
class Result:
    check: Check
    # `None` when done.
    issue: Issue | None


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


async def checks(db: AsyncSession, tenant: Tenant) -> list[Result]:
    return [
        Result("name", None if (tenant.name or "").strip() else "missing"),
        Result("mediator", await _mediator(db, tenant)),
        Result("signing_flow", await _signing_flow(db, tenant)),
    ]


def score(results: list[Result]) -> int:
    """The share of checks done, as a whole percentage."""
    done = sum(1 for result in results if result.issue is None)
    return round(100 * done / len(results)) if results else 100
