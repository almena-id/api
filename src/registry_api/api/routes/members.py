"""Who belongs to a tenant, and inviting more people into it.

Any member sees the list; only an admin invites. An invitation names an email
and a role, is announced by email, and turns into membership the next time
that address signs in, however it signs in. Admins take an invitation back,
change a member's role and remove members; anyone may leave. A tenant always
keeps an admin, and whoever signs for it (as the tenant, or as one of its
issuers or verifiers) stays until somebody else is chosen. An account left
with no tenant is given one of its own, as when it signed up.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.api.routes.auth import CurrentSession, DbSession, own_tenant
from registry_api.api.routes.directory import TenantId
from registry_api.mail import Locale, Mailer, get_mailer
from registry_api.models import (
    Issuer,
    Role,
    Tenant,
    TenantInvitation,
    TenantMember,
    User,
    UserIdentity,
    Verifier,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["members"])


class MemberOut(BaseModel):
    # `member` has signed in and belongs; `invited` has not signed in yet.
    status: Literal["member", "invited"]
    # Members only: their account, and what they like to be called.
    user_id: uuid.UUID | None = None
    # Invitations only: what takes it back.
    invitation_id: uuid.UUID | None = None
    alias: str | None = None
    # `null` for a member whose account has no email.
    email: str | None
    role: Role
    # When they joined, or when they were invited.
    since: datetime
    # Members only: whether their account has an Almena wallet linked, which
    # everybody needs to work in the platform (signing starts there).
    wallet: bool | None = None


class InvitationIn(BaseModel):
    email: EmailStr
    role: Role
    # The language the email is written in: the inviter's portal's.
    locale: Locale = "en"


async def admin_of(tenant_id: TenantId, session: CurrentSession, db: DbSession) -> uuid.UUID:
    member = await db.get(TenantMember, (tenant_id, session.user_id))
    if member is None or member.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_admin")
    return tenant_id


@router.get("/members", summary="The tenant's members, then those invited and not yet in")
async def list_members(tenant_id: TenantId, db: DbSession) -> list[MemberOut]:
    wallet = (
        select(UserIdentity.id)
        .where(UserIdentity.user_id == User.id, UserIdentity.provider == "almena")
        .exists()
    )
    members = await db.execute(
        select(User.id, User.alias, User.email, TenantMember.role, TenantMember.created_at, wallet)
        .join(User, User.id == TenantMember.user_id)
        .where(TenantMember.tenant_id == tenant_id)
        .order_by(TenantMember.created_at, User.email)
    )
    invited = await db.scalars(
        select(TenantInvitation)
        .where(TenantInvitation.tenant_id == tenant_id)
        .order_by(TenantInvitation.created_at, TenantInvitation.email)
    )
    return [
        MemberOut(
            status="member",
            user_id=user_id,
            alias=alias,
            email=email,
            role=role,
            since=since,
            wallet=linked,
        )
        for user_id, alias, email, role, since, linked in members
    ] + [
        MemberOut(
            status="invited",
            invitation_id=i.id,
            email=i.email,
            role=i.role,
            since=i.created_at,
        )
        for i in invited
    ]


@router.post(
    "/invitations",
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody by email, with a role (admins only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`"},
        status.HTTP_409_CONFLICT: {"description": "`already_member`"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "`mail_unavailable`"},
    },
)
async def invite(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)],
    body: InvitationIn,
    session: CurrentSession,
    db: DbSession,
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> MemberOut:
    email = body.email.lower()
    already = await db.scalar(
        select(TenantMember)
        .join(User, User.id == TenantMember.user_id)
        .where(TenantMember.tenant_id == tenant_id, User.email == email)
    )
    if already is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already_member")

    # Inviting the same address again updates the role and sends the email again.
    invitation = await db.scalar(
        select(TenantInvitation).where(
            TenantInvitation.tenant_id == tenant_id, TenantInvitation.email == email
        )
    )
    if invitation is None:
        invitation = TenantInvitation(tenant_id=tenant_id, email=email, role=body.role)
        db.add(invitation)
    invitation.role = body.role
    invitation.invited_by = session.user_id
    await db.flush()
    await db.refresh(invitation)

    tenant = await db.get_one(Tenant, tenant_id)
    try:
        await mailer.send_invitation(
            email,
            tenant=tenant.name,
            inviter=session.user.alias or session.user.email,
            role=body.role,
            locale=body.locale,
        )
    except OSError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="mail_unavailable"
        ) from None
    await db.commit()
    return MemberOut(
        status="invited",
        invitation_id=invitation.id,
        email=invitation.email,
        role=body.role,
        since=invitation.created_at,
    )


class RoleIn(BaseModel):
    role: Role


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail=code)


async def _member(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> TenantMember:
    member = await db.get(TenantMember, (tenant_id, user_id))
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="member_not_found")
    return member


async def _keeps_an_admin(db: AsyncSession, member: TenantMember) -> None:
    """Refuse (`last_admin`) when `member` is the tenant's only admin."""
    if member.role != "admin":
        return
    admins = await db.scalar(
        select(func.count())
        .select_from(TenantMember)
        .where(TenantMember.tenant_id == member.tenant_id, TenantMember.role == "admin")
    )
    if (admins or 0) <= 1:
        raise _refuse("last_admin")


async def _signs_for_it(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether the user is named to sign: as the tenant (`single_user`), or as
    one of its issuers or verifiers."""
    tenant = await db.get_one(Tenant, tenant_id)
    if tenant.signing_flow == "single_user" and tenant.signer_id == user_id:
        return True
    for model in (Issuer, Verifier):
        named = await db.scalar(
            select(model.id)
            .where(model.tenant_id == tenant_id, model.signer_id == user_id)
            .limit(1)
        )
        if named is not None:
            return True
    return False


@router.patch(
    "/members/{user_id}",
    summary="Change a member's role (admins only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`"},
        status.HTTP_404_NOT_FOUND: {"description": "`member_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`last_admin`: it would be left with none"},
    },
)
async def change_role(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)],
    user_id: uuid.UUID,
    body: RoleIn,
    db: DbSession,
) -> MemberOut:
    member = await _member(db, tenant_id, user_id)
    if body.role != "admin":
        await _keeps_an_admin(db, member)
    member.role = body.role
    await db.commit()
    return next(m for m in await list_members(tenant_id, db) if m.user_id == user_id)


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member (admins), or leave (anyone, naming themselves)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`: removing somebody else"},
        status.HTTP_404_NOT_FOUND: {"description": "`member_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`last_admin`: it would be left with none; `member_signs`: "
            "they sign as the tenant, or as one of its issuers or verifiers — choose "
            "somebody else first"
        },
    },
)
async def remove_member(
    tenant_id: TenantId, user_id: uuid.UUID, session: CurrentSession, db: DbSession
) -> Response:
    if user_id != session.user_id:
        await admin_of(tenant_id, session, db)
    member = await _member(db, tenant_id, user_id)
    await _keeps_an_admin(db, member)
    if await _signs_for_it(db, tenant_id, user_id):
        raise _refuse("member_signs")
    await db.delete(member)
    await db.flush()
    remaining = await db.scalar(
        select(TenantMember.tenant_id).where(TenantMember.user_id == user_id).limit(1)
    )
    if remaining is None:
        # Nobody is left without somewhere to work.
        user = await db.get_one(User, user_id)
        await own_tenant(db, user, "en")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take an invitation back before it is accepted (admins only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_admin`"},
        status.HTTP_404_NOT_FOUND: {"description": "`invitation_not_found`"},
    },
)
async def cancel_invitation(
    tenant_id: Annotated[uuid.UUID, Depends(admin_of)], invitation_id: uuid.UUID, db: DbSession
) -> Response:
    invitation = await db.get(TenantInvitation, invitation_id)
    if invitation is None or invitation.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="invitation_not_found")
    await db.delete(invitation)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
