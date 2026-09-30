"""Who belongs to a tenant, and inviting more people into it.

Any member sees the list; only an admin invites. An invitation names an email
and a role, is announced by email, and turns into membership the next time
that address signs in, however it signs in.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.mail import Locale, Mailer, get_mailer
from registry_api.models import Role, Tenant, TenantInvitation, TenantMember, User, UserIdentity

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["members"])


class MemberOut(BaseModel):
    # `member` has signed in and belongs; `invited` has not signed in yet.
    status: Literal["member", "invited"]
    # Members only: their account, and what they like to be called.
    user_id: uuid.UUID | None = None
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
        MemberOut(status="invited", email=i.email, role=i.role, since=i.created_at) for i in invited
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
        email=invitation.email,
        role=body.role,
        since=invitation.created_at,
    )
