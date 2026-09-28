"""Almena's reviewers: the members of the root tenant (`registry_api.root`).

They see the certification requests sent for review, oldest first, and approve
or reject each; approving one puts it in force and supersedes the tenant's
previous certification.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import undefer

from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.certification import CertificationOut, certification_out
from registry_api.models import Certification, Tenant
from registry_api.root import is_reviewer

router = APIRouter(prefix="/review", tags=["review"])


async def reviewer(session: CurrentSession, db: DbSession) -> uuid.UUID:
    if not await is_reviewer(db, session.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_reviewer")
    return session.user_id


Reviewer = Annotated[uuid.UUID, Depends(reviewer)]


class Pending(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str | None
    legal_name: str | None
    domain: str | None
    submitted_at: datetime | None


class ReviewDetail(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str | None
    request: CertificationOut
    # What the tenant holds today, to compare with.
    current: CertificationOut | None


class Rejection(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/certifications", summary="Requests waiting for review, oldest first")
async def pending(_: Reviewer, db: DbSession) -> list[Pending]:
    rows = await db.execute(
        select(Certification, Tenant.name)
        .join(Tenant, Tenant.id == Certification.tenant_id)
        .where(Certification.status == "in_review")
        .order_by(Certification.submitted_at, Certification.id)
    )
    return [
        Pending(
            id=item.id,
            tenant_id=item.tenant_id,
            tenant_name=name,
            legal_name=item.legal_name,
            domain=item.domain,
            submitted_at=item.submitted_at,
        )
        for item, name in rows
    ]


async def _request(db: DbSession, certification_id: uuid.UUID) -> Certification:
    item = await db.scalar(
        select(Certification)
        .options(undefer(Certification.logo))
        .where(Certification.id == certification_id)
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="certification_not_found")
    return item


async def _pending(db: DbSession, certification_id: uuid.UUID) -> Certification:
    item = await _request(db, certification_id)
    if item.status != "in_review":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not_in_review")
    return item


@router.get("/certifications/{certification_id}", summary="One request, beside what is in force")
async def detail(certification_id: uuid.UUID, _: Reviewer, db: DbSession) -> ReviewDetail:
    item = await _request(db, certification_id)
    tenant = await db.get_one(Tenant, item.tenant_id)
    current = await db.scalar(
        select(Certification)
        .options(undefer(Certification.logo))
        .where(Certification.tenant_id == item.tenant_id, Certification.status == "approved")
    )
    return ReviewDetail(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        request=certification_out(item),
        current=certification_out(current) if current and current.id != item.id else None,
    )


@router.post(
    "/certifications/{certification_id}/approve",
    summary="Approve: it is in force, and supersedes the previous one",
    responses={status.HTTP_409_CONFLICT: {"description": "`not_in_review`"}},
)
async def approve(
    certification_id: uuid.UUID, user_id: Reviewer, db: DbSession
) -> CertificationOut:
    item = await _pending(db, certification_id)
    await db.execute(
        update(Certification)
        .where(Certification.tenant_id == item.tenant_id, Certification.status == "approved")
        .values(status="superseded")
    )
    item.status = "approved"
    item.reviewed_at = datetime.now(UTC)
    item.reviewed_by = user_id
    await db.commit()
    return certification_out(await _request(db, certification_id))


@router.post(
    "/certifications/{certification_id}/reject",
    summary="Reject, saying why; the one in force, if any, stays",
    responses={status.HTTP_409_CONFLICT: {"description": "`not_in_review`"}},
)
async def reject(
    certification_id: uuid.UUID, body: Rejection, user_id: Reviewer, db: DbSession
) -> CertificationOut:
    item = await _pending(db, certification_id)
    item.status = "rejected"
    item.reason = body.reason.strip()
    item.reviewed_at = datetime.now(UTC)
    item.reviewed_by = user_id
    await db.commit()
    return certification_out(await _request(db, certification_id))
