"""How an issuer or a verifier signs: its signing system.

Nobody signs on the server; people do, from their wallets. This is where the
tenant says who. The catalogue so far has one system:

- ``single_user``: one member of the tenant, named here, signs alone.

Any member reads it; admins set it. The signer must be a member of the tenant
when chosen. `null` means not configured yet.
"""

import uuid
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.models import Issuer, TenantMember, User, Verifier

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["signing"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]
Kind = Literal["issuers", "verifiers"]
System = Literal["single_user"]

MODELS: dict[str, type[Issuer | Verifier]] = {"issuers": Issuer, "verifiers": Verifier}


class Signer(BaseModel):
    id: uuid.UUID
    email: str
    alias: str | None
    # Whether they still belong to the tenant: a signer who left signs nothing.
    member: bool


class SigningOut(BaseModel):
    system: System | None
    # `single_user`: who signs.
    signer: Signer | None


class SigningIn(BaseModel):
    # `null` takes the configuration away.
    system: System | None
    # `single_user`: the member who signs.
    user_id: uuid.UUID | None = None


async def _owned(
    db: DbSession, kind: Kind, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Issuer | Verifier:
    item = cast(Issuer | Verifier | None, await db.get(MODELS[kind], item_id))
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{kind[:-1]}_not_found")
    return item


async def _out(db: DbSession, item: Issuer | Verifier) -> SigningOut:
    signer = None
    if item.signer_id is not None:
        user = await db.get(User, item.signer_id)
        if user is not None:
            member = await db.get(TenantMember, (item.tenant_id, user.id))
            signer = Signer(
                id=user.id, email=user.email, alias=user.alias, member=member is not None
            )
    return SigningOut(system=cast(System | None, item.signing), signer=signer)


@router.get("/{kind}/{item_id}/signing", summary="How an issuer or verifier signs")
async def get_signing(
    tenant_id: TenantId, kind: Kind, item_id: uuid.UUID, db: DbSession
) -> SigningOut:
    return await _out(db, await _owned(db, kind, tenant_id, item_id))


@router.put(
    "/{kind}/{item_id}/signing",
    summary="Set how an issuer or verifier signs (admins only)",
    responses={
        403: {"description": "`not_admin`"},
        422: {"description": "`signer_required`, `signer_not_member`"},
    },
)
async def set_signing(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, body: SigningIn, db: DbSession
) -> SigningOut:
    item = await _owned(db, kind, tenant_id, item_id)
    if body.system is None:
        item.signing, item.signer_id = None, None
    else:
        if body.user_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="signer_required")
        member = await db.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant_id, TenantMember.user_id == body.user_id
            )
        )
        if member is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="signer_not_member")
        item.signing, item.signer_id = body.system, body.user_id
    await db.commit()
    return await _out(db, item)
