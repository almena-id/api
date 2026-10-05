"""What waits in a tenant to be signed or published, in the order it is done.

Everything the tenant puts out is signed by people from their wallets, and
most of it is signed before anything else can follow: an identity's DID
before what acts as it is published, an issuer's status list before its
credentials. One list says what waits, why, what has to be done first and
whether it is the asker's to do:

- an identity whose DID is ``pending`` (not signed yet) or ``outdated``
  (changes to sign), signed by whoever signs as the tenant;
- an issuer, verifier or mediator still a ``draft``, or published with an
  endorsement that ``expired``, published (again) by them too;
- an issuer's status list ``unsigned``, or ``outdated`` (signed with a key its
  DID no longer lists), signed by the issuer's signer;
- an ``accepted`` application's credential, signed — issued — by the
  issuer's signer.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import dids, signing_flows, status_lists
from registry_api.api.routes.auth import CurrentSession, DbSession, as_utc
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.issuance import status_list_for
from registry_api.config import get_settings
from registry_api.models import Application, Identity, Issuer, Mediator, Tenant, Verifier

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["pending"])

Kind = Literal["identity", "issuer", "verifier", "mediator", "status_list", "credential"]
State = Literal["pending", "outdated", "draft", "expired", "unsigned", "accepted"]
# What has to be done first: its identity signed, the tenant's own, a signer
# named for the issuer, the issuer's status list signed.
Blocker = Literal["identity_pending", "tenant_pending", "signer_needed", "status_list_unsigned"]


class IssuerRef(BaseModel):
    id: uuid.UUID
    name: str


class Pending(BaseModel):
    kind: Kind
    # `sign` (an identity's DID, a status list, a credential) or `publish`.
    action: Literal["sign", "publish"]
    # The identity, issuer, verifier, mediator, status list or application.
    id: uuid.UUID
    # Its name; a status list's and an application's slug.
    name: str
    state: State
    # Status lists and credentials: the issuer whose they are.
    issuer: IssuerRef | None = None
    # Credentials: the type to issue (`custom:{key}` for a tenant's own).
    credential_type: str | None = None
    blocked_by: Blocker | None = None
    # Whether the asker does it: who signs as the tenant (identities,
    # publishing), the issuer's signer (its status lists and credentials).
    yours: bool


Publishable = Literal["issuer", "verifier", "mediator"]
PUBLISHABLE: dict[Publishable, type[Issuer | Verifier | Mediator]] = {
    "issuer": Issuer,
    "verifier": Verifier,
    "mediator": Mediator,
}


def _signer_needed(issuer: Issuer) -> bool:
    return issuer.signing != "single_user" or issuer.signer_id is None


def _issuers_signer(issuer: Issuer, user_id: uuid.UUID) -> bool:
    return issuer.signing == "single_user" and issuer.signer_id == user_id


async def _identities(db: AsyncSession, tenant_id: uuid.UUID, signs: bool) -> list[Pending]:
    did_url = get_settings().did_url
    found = await db.scalars(
        select(Identity)
        .where(Identity.tenant_id == tenant_id)
        .order_by(Identity.created_at, Identity.id)
    )
    rows = []
    for identity in found:
        state = await dids.status_of(db, did_url, identity)
        if state != "signed":
            rows.append(
                Pending(
                    kind="identity",
                    action="sign",
                    id=identity.id,
                    name=identity.name,
                    state=state,
                    yours=signs,
                )
            )
    return rows


async def _publications(
    db: AsyncSession, tenant: Tenant, owner: Identity | None, signs: bool
) -> list[Pending]:
    now = datetime.now(UTC)
    rows: list[tuple[datetime, Pending]] = []
    for kind, model in PUBLISHABLE.items():
        found = cast(
            list[Issuer | Verifier | Mediator],
            list(await db.scalars(select(model).where(model.tenant_id == tenant.id))),
        )
        for item in found:
            identity = item.identity
            if item.published_at is None:
                state: State = "draft"
            elif identity.endorsed_until and as_utc(identity.endorsed_until) <= now:
                state = "expired"
            else:
                continue
            # As publishing refuses: its own DID first, then the tenant's.
            blocked: Blocker | None = None
            if identity.did is None:
                blocked = "identity_pending"
            elif owner is None or owner.did is None:
                blocked = "tenant_pending"
            row = Pending(
                kind=kind,
                action="publish",
                id=item.id,
                name=item.name,
                state=state,
                blocked_by=blocked,
                yours=signs,
            )
            rows.append((as_utc(item.created_at), row))
    rows.sort(key=lambda pair: (pair[0], str(pair[1].id)))
    return [row for _, row in rows]


async def _issuers_work(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[list[Pending], list[Pending]]:
    """Its issuers' status lists to sign, and the credentials to issue."""
    issuers = {
        issuer.id: issuer
        for issuer in await db.scalars(
            select(Issuer)
            .where(Issuer.tenant_id == tenant_id)
            .order_by(Issuer.created_at, Issuer.id)
        )
    }
    # Whether each list is signed: asked once, however many credentials go in it.
    signed: dict[uuid.UUID, bool] = {}
    lists = []
    for issuer in issuers.values():
        for item in await status_lists.lists_of(db, issuer):
            signed[item.id] = not await status_lists.needs_signing(db, item, issuer)
            if signed[item.id]:
                continue
            lists.append(
                Pending(
                    kind="status_list",
                    action="sign",
                    id=item.id,
                    name=item.slug,
                    state="unsigned" if item.token is None else "outdated",
                    issuer=IssuerRef(id=issuer.id, name=issuer.name),
                    blocked_by="signer_needed" if _signer_needed(issuer) else None,
                    yours=_issuers_signer(issuer, user_id),
                )
            )
    accepted = await db.scalars(
        select(Application)
        .where(Application.tenant_id == tenant_id, Application.status == "accepted")
        .order_by(Application.decided_at, Application.id)
    )
    credentials = []
    for application in accepted:
        issuer = issuers[application.issuer_id]
        listed = await status_list_for(db, application, issuer)
        blocked: Blocker | None = None
        if _signer_needed(issuer):
            blocked = "signer_needed"
        elif listed is None or not signed.get(listed.id):
            blocked = "status_list_unsigned"
        credentials.append(
            Pending(
                kind="credential",
                action="sign",
                id=application.id,
                name=application.slug,
                state="accepted",
                issuer=IssuerRef(id=issuer.id, name=issuer.name),
                credential_type=application.credential_type,
                blocked_by=blocked,
                yours=_issuers_signer(issuer, user_id),
            )
        )
    return lists, credentials


@router.get(
    "/pending",
    summary="What waits to be signed or published: identities, then issuers, verifiers "
    "and mediators, then issuers' status lists and credentials",
)
async def pending(tenant_id: TenantId, session: CurrentSession, db: DbSession) -> list[Pending]:
    tenant = await db.get_one(Tenant, tenant_id)
    owner = await db.get(Identity, tenant.identity_id) if tenant.identity_id else None
    signs = await signing_flows.signs(db, tenant, session.user_id)
    lists, credentials = await _issuers_work(db, tenant_id, session.user_id)
    return [
        *await _identities(db, tenant_id, signs),
        *await _publications(db, tenant, owner, signs),
        *lists,
        *credentials,
    ]
