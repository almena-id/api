"""The issuer's inbox: the applications its tenant's issuers received —
submitted or decided — a page at a time, one with what the holder signed and
whether that holds, its files, and the decision (`routes.applications` for
the holder's side)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import (
    paging,
    queues,
    status_lists,
)
from registry_api.api.routes.applications import (
    check_signature,
    content,
    content_digest,
    credential_status,
    files_of,
    parts,
)
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.broker import Broker, get_broker
from registry_api.models import (
    Application,
    ApplicationFile,
    Form,
    Issuer,
    StatusList,
)

router = APIRouter(prefix="/tenants/{tenant_id}/applications", tags=["applications"])


def _refuse(code: str, status_code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code, detail=code)


# The issuer's inbox ----------------------------------------------------------


async def received(
    db: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> Application:
    item = await db.get(Application, application_id)
    if item is None or item.tenant_id != tenant_id or item.status in ("open", "paired"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="application_not_found")
    return item


@router.get(
    "",
    summary="The applications the tenant's issuers received, newest first, a page at a time",
    responses={status.HTTP_400_BAD_REQUEST: {"description": "`invalid_cursor`"}},
)
async def list_received(
    tenant_id: TenantId,
    db: DbSession,
    issuer_id: uuid.UUID | None = None,
    limit: paging.Limit = 20,
    cursor: paging.Cursor = None,
) -> dict[str, Any]:
    query = select(Application).where(
        Application.tenant_id == tenant_id, Application.status.not_in(("open", "paired"))
    )
    if issuer_id is not None:
        query = query.where(Application.issuer_id == issuer_id)
    if cursor:
        query = query.where(paging.after(Application.submitted_at, Application.id, cursor))
    rows = list(
        await db.scalars(
            query.order_by(Application.submitted_at.desc(), Application.id.desc()).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    # Their issuers and forms, once each: got by key afterwards, the session answers.
    for model, ids in ((Issuer, {r.issuer_id for r in rows}), (Form, {r.form_id for r in rows})):
        if ids:
            await db.scalars(select(model).where(model.id.in_(ids)))
    out = []
    for item in rows:
        issuer, form = await parts(db, item)
        out.append(
            {
                "id": str(item.id),
                "slug": item.slug,
                "status": item.status,
                "issuer": {"id": str(issuer.id), "name": issuer.name},
                "credential_type": item.credential_type,
                "form": form.name,
                "holder_did": item.holder_did,
                "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
                "credential_status": item.credential_status,
            }
        )
    last = rows[-1] if rows else None
    return {
        "items": out,
        "next_cursor": paging.encode(last.submitted_at, last.id)
        if more and last and last.submitted_at
        else None,
    }


@router.get(
    "/{application_id}",
    summary="One received application: what the holder signed, and whether it holds",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`application_not_found`"}},
)
async def get_received(
    tenant_id: TenantId, application_id: uuid.UUID, db: DbSession
) -> dict[str, Any]:
    item = await received(db, tenant_id, application_id)
    issuer, form = await parts(db, item)
    signed = await content(db, item)
    try:
        claims = check_signature(str(item.signature), str(item.holder_did), None)
        signature_valid = claims.get("digest") == content_digest(signed) == item.digest
    except HTTPException:
        signature_valid = False
    listed = await db.get(StatusList, item.status_list_id) if item.status_list_id else None
    return {
        "id": str(item.id),
        "slug": item.slug,
        "status": item.status,
        "issuer": {"id": str(issuer.id), "name": issuer.name},
        "form": {"id": str(form.id), "name": form.name},
        "holder_did": item.holder_did,
        "content": signed,
        "files": await files_of(db, item),
        "presented": item.presented,
        "digest": item.digest,
        "signature": item.signature,
        "signature_valid": signature_valid,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "decision_note": item.decision_note,
        "issued_at": item.issued_at.isoformat() if item.issued_at else None,
        "valid_until": item.credential_valid_until.isoformat()
        if item.credential_valid_until
        else None,
        "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
        **credential_status(item),
        # Its entry in the issuer's status list; `null` if issued before them.
        "status_list": {"uri": status_lists.uri(listed), "index": item.status_index}
        if listed is not None
        else None,
        # The statuses its issuer's signer may give it next; none unless issued
        # into a status list, nor once revoked.
        "credential_statuses": status_lists.next_statuses(item.credential_status)
        if item.status == "issued" and listed is not None
        else [],
    }


@router.get(
    "/{application_id}/files/{key}",
    summary="A file the holder uploaded, as uploaded",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`file_not_found`"}},
)
async def get_file(
    tenant_id: TenantId, application_id: uuid.UUID, key: str, db: DbSession
) -> Response:
    item = await received(db, tenant_id, application_id)
    row = await db.scalar(
        select(ApplicationFile).where(
            ApplicationFile.application_id == item.id, ApplicationFile.key == key
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="file_not_found")
    return Response(
        row.data,
        media_type=row.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


class DecisionIn(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


@router.post(
    "/{application_id}/decision",
    summary="Accept or reject a submitted application (any member)",
    responses={status.HTTP_409_CONFLICT: {"description": "`already_decided`"}},
)
async def decide(
    tenant_id: TenantId,
    application_id: uuid.UUID,
    body: DecisionIn,
    db: DbSession,
    broker: Annotated[Broker, Depends(get_broker)],
) -> dict[str, Any]:
    item = await received(db, tenant_id, application_id)
    if item.status != "submitted":
        raise _refuse("already_decided")
    item.status = body.decision
    item.decision_note = (body.note or "").strip() or None
    item.decided_at = datetime.now(UTC)
    await db.commit()
    await queues.decided(db, broker, item)
    return await get_received(tenant_id, application_id, db)
