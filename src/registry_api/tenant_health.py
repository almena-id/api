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

The root's list is its onboarding instead, in the order it can be done —
each step needs the ones before it:

- ``name``;
- ``signing_flow``: a linked wallet, whose key signs everything after;
- ``identity``: its own DID signed; ``outdated`` once its document changes —
  above all when it chooses a mediator, whose service it then names.

Neither a domain nor a mediator of its own is asked of any tenant: both are
optional (a mediator of its own needs a verified domain to listen under).

Then, every tenant, root or not, once it has issuers or verifiers (none
asks nothing of a tenant that has none yet), with the first one each is done
on (`item`, `item_kind`):

- ``signers``: each issuer and verifier has its signer (``no_signer``: none
  named, or one who left the tenant) and that signer a linked wallet
  (``no_wallet``);
- ``status_lists``: each published issuer has its lists signed
  (``unsigned``: never, or none yet while it grants credential types) by a
  key its DID still lists (``stale``);
- ``queues``: each has its queue at the broker (``missing``).

And a tenant other than the root has something published (``published``:
``missing`` while it has no issuer or verifier, ``unpublished`` while none of
them is published).

New checks are added here, in the order the portal lists them.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import dids, signing_flows, status_lists
from registry_api.config import get_settings
from registry_api.models import (
    Identity,
    Issuer,
    Mediator,
    Tenant,
    TenantMember,
    UserIdentity,
    Verifier,
)

Check = Literal[
    "name",
    "signing_flow",
    "identity",
    "mediator",
    "signers",
    "status_lists",
    "queues",
    "published",
]
ItemKind = Literal["identity", "issuer", "verifier"]
Issue = Literal[
    "missing",
    "pending",
    "outdated",
    "unsigned",
    "unpublished",
    "no_signer",
    "no_wallet",
    "stale",
]


@dataclass(frozen=True)
class Result:
    check: Check
    # `None` when done.
    issue: Issue | None
    # What the task is done on, when it is one thing: the tenant's identity
    # to sign, or an issuer or verifier.
    item: uuid.UUID | None = None
    # What `item` is.
    item_kind: ItemKind | None = None


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


async def _identity(db: AsyncSession, tenant: Tenant) -> Result:
    identity = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    if identity is None:
        return Result("identity", "pending")
    state = await dids.status_of(db, get_settings().did_url, identity)
    issue: Issue | None = None if state == "signed" else state
    return Result("identity", issue, identity.id, "identity")


Component = Issuer | Verifier


async def _components(db: AsyncSession, tenant: Tenant) -> list[Component]:
    """The tenant's issuers, then its verifiers, oldest first."""
    found: list[Component] = []
    for model in (Issuer, Verifier):
        found += await db.scalars(
            select(model).where(model.tenant_id == tenant.id).order_by(model.created_at, model.id)
        )
    return found


def _kind(item: Component) -> ItemKind:
    return "issuer" if isinstance(item, Issuer) else "verifier"


def _first(check: Check, failing: list[tuple[Issue, Component]]) -> Result:
    if not failing:
        return Result(check, None)
    issue, item = failing[0]
    return Result(check, issue, item.id, _kind(item))


async def _signers(db: AsyncSession, items: list[Component]) -> Result:
    failing: list[tuple[Issue, Component]] = []
    for item in items:
        if (
            item.signing != "single_user"
            or item.signer_id is None
            or await db.get(TenantMember, (item.tenant_id, item.signer_id)) is None
        ):
            failing.append(("no_signer", item))
        elif (
            await db.scalar(
                select(UserIdentity.id)
                .where(UserIdentity.user_id == item.signer_id, UserIdentity.provider == "almena")
                .limit(1)
            )
            is None
        ):
            failing.append(("no_wallet", item))
    return _first("signers", failing)


async def _status_lists(db: AsyncSession, items: list[Component]) -> Result:
    failing: list[tuple[Issue, Component]] = []
    for item in items:
        if not isinstance(item, Issuer) or item.published_at is None or item.identity.did is None:
            # A draft's DID does not resolve: no list of it can be checked.
            continue
        lists = await status_lists.lists_of(db, item)
        if not lists:
            if item.credential_types:
                failing.append(("unsigned", item))
            continue
        for listed in lists:
            if listed.token is None:
                failing.append(("unsigned", item))
                break
            if await status_lists.needs_signing(db, listed, item):
                failing.append(("stale", item))
                break
    return _first("status_lists", failing)


def _queues(items: list[Component]) -> Result:
    return _first("queues", [("missing", item) for item in items if item.queue_created_at is None])


def _published(items: list[Component]) -> Result:
    if not items:
        return Result("published", "missing")
    if any(item.published_at is not None for item in items):
        return Result("published", None)
    return Result("published", "unpublished", items[0].id, _kind(items[0]))


async def _operating(db: AsyncSession, items: list[Component]) -> list[Result]:
    """What its issuers and verifiers need to work; nothing while it has none."""
    if not items:
        return []
    return [await _signers(db, items), await _status_lists(db, items), _queues(items)]


async def checks(db: AsyncSession, tenant: Tenant) -> list[Result]:
    items = await _components(db, tenant)
    if tenant.root:
        return [
            Result("name", None if (tenant.name or "").strip() else "missing"),
            Result("signing_flow", await _signing_flow(db, tenant)),
            await _identity(db, tenant),
            *await _operating(db, items),
        ]
    return [
        Result("name", None if (tenant.name or "").strip() else "missing"),
        Result("mediator", await _mediator(db, tenant)),
        Result("signing_flow", await _signing_flow(db, tenant)),
        *await _operating(db, items),
        _published(items),
    ]


def score(results: list[Result]) -> int:
    """The share of checks done, as a whole percentage."""
    done = sum(1 for result in results if result.issue is None)
    return round(100 * done / len(results)) if results else 100
