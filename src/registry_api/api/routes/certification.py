"""A tenant's certification by Almena, as the tenant asks for it.

The tenant's admins fill in a request — legal name, domain, logo — prove the
domain with a DNS TXT record, and send it for review; members see it. The
certification in force stays so while a new request is worked on. Almena's
reviewers take it from there (see `review`).
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import undefer

from registry_api import certification as checks
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.models import Certification
from registry_api.models.certification import OPEN

router = APIRouter(prefix="/tenants/{tenant_id}/certification", tags=["certification"])
public = APIRouter(prefix="/certifications", tags=["certification"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]
TxtLookup = Annotated[checks.TxtLookup, Depends(checks.get_txt_lookup)]


class DnsRecord(BaseModel):
    type: str = "TXT"
    name: str
    value: str


class CertificationOut(BaseModel):
    id: uuid.UUID
    status: str
    legal_name: str | None
    domain: str | None
    # What to publish to prove the domain; `null` until there is one.
    dns_record: DnsRecord | None
    domain_verified: bool
    # The logo as a `data:` URL, for the portal to show as it is.
    logo: str | None
    created_at: datetime
    submitted_at: datetime | None
    reviewed_at: datetime | None
    # Why it was rejected.
    reason: str | None


class CertificationState(BaseModel):
    # The certification in force, if any.
    current: CertificationOut | None
    # The request being worked on or reviewed, if any.
    request: CertificationOut | None


class RequestIn(BaseModel):
    # Each field is changed only when sent.
    legal_name: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=300)


def certification_out(item: Certification) -> CertificationOut:
    record = None
    if item.domain and item.dns_token:
        name, value = checks.dns_record(item.domain, item.dns_token)
        record = DnsRecord(name=name, value=value)
    logo = None
    if item.logo is not None and item.logo_type:
        logo = f"data:{item.logo_type};base64,{base64.b64encode(item.logo).decode()}"
    return CertificationOut(
        id=item.id,
        status=item.status,
        legal_name=item.legal_name,
        domain=item.domain,
        dns_record=record,
        domain_verified=item.domain_verified_at is not None,
        logo=logo,
        created_at=item.created_at,
        submitted_at=item.submitted_at,
        reviewed_at=item.reviewed_at,
        reason=item.reason,
    )


async def _latest(
    db: DbSession, tenant_id: uuid.UUID, statuses: tuple[str, ...]
) -> Certification | None:
    return await db.scalar(
        select(Certification)
        .options(undefer(Certification.logo))
        .where(Certification.tenant_id == tenant_id, Certification.status.in_(statuses))
        .order_by(Certification.created_at.desc())
        .limit(1)
    )


async def _state(db: DbSession, tenant_id: uuid.UUID) -> CertificationState:
    current = await _latest(db, tenant_id, ("approved",))
    request = await _latest(db, tenant_id, OPEN)
    return CertificationState(
        current=certification_out(current) if current else None,
        request=certification_out(request) if request else None,
    )


async def _editable(db: DbSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Certification:
    """The request to change: the open one, or a new one starting from what is in force."""
    request = await _latest(db, tenant_id, OPEN)
    if request is not None:
        if request.status == "in_review":
            raise HTTPException(status.HTTP_409_CONFLICT, detail="in_review")
        # Changing a rejected request is working on it again.
        request.status = "draft"
        request.reason = None
        return request
    current = await _latest(db, tenant_id, ("approved",))
    request = Certification(tenant_id=tenant_id, status="draft", created_by=user_id)
    if current is not None:
        request.legal_name = current.legal_name
        request.domain = current.domain
        request.dns_token = current.dns_token
        request.domain_verified_at = current.domain_verified_at
        request.logo = current.logo
        request.logo_type = current.logo_type
    request.created_at = datetime.now(UTC)
    db.add(request)
    return request


def _fail(error: checks.CertificationError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code)


@router.get("", summary="The certification in force and the request being worked on")
async def get_certification(tenant_id: TenantId, db: DbSession) -> CertificationState:
    return await _state(db, tenant_id)


@router.put(
    "/request",
    summary="Fill in the request: legal name and/or domain (admins only)",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`in_review`: wait for the review"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`legal_name_required`, `domain_invalid`"
        },
    },
)
async def update_request(
    tenant_id: AdminTenant, body: RequestIn, session: CurrentSession, db: DbSession
) -> CertificationState:
    sent = body.model_fields_set
    legal_name = domain = None
    if "legal_name" in sent:
        legal_name = (body.legal_name or "").strip()
        if not legal_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "legal_name_required")
    if "domain" in sent:
        try:
            domain = checks.domain(body.domain or "")
        except checks.CertificationError as error:
            raise _fail(error) from None
    request = await _editable(db, tenant_id, session.user_id)
    if legal_name is not None:
        request.legal_name = legal_name
    if domain is not None and domain != request.domain:
        # Another domain, another proof.
        request.domain = domain
        request.dns_token = checks.new_token()
        request.domain_verified_at = None
    await db.commit()
    return await _state(db, tenant_id)


@router.put(
    "/request/logo",
    summary="Upload the logo: the request body is the image (admins only)",
    openapi_extra={
        "requestBody": {
            "content": {
                t: {"schema": {"type": "string", "format": "binary"}}
                for t in (
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                )
            }
        }
    },
    responses={
        status.HTTP_409_CONFLICT: {"description": "`in_review`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`logo_invalid` (PNG, JPEG or WebP), `logo_too_large` (256 KB)"
        },
    },
)
async def upload_logo(
    tenant_id: AdminTenant, request: Request, session: CurrentSession, db: DbSession
) -> CertificationState:
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > checks.LOGO_MAX_BYTES:
            break
    try:
        media_type = checks.logo_type(data)
    except checks.CertificationError as error:
        raise _fail(error) from None
    item = await _editable(db, tenant_id, session.user_id)
    item.logo, item.logo_type = data, media_type
    await db.commit()
    return await _state(db, tenant_id)


@router.post(
    "/request/check-domain",
    summary="Look for the DNS TXT record that proves the domain (admins only)",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`in_review`, `no_request`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`domain_required`, `dns_record_not_found`, `dns_unavailable`"
        },
    },
)
async def check_domain(
    tenant_id: AdminTenant, db: DbSession, lookup: TxtLookup
) -> CertificationState:
    request = await _latest(db, tenant_id, OPEN)
    if request is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no_request")
    if request.status == "in_review":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="in_review")
    if not request.domain or not request.dns_token:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="domain_required")
    name, value = checks.dns_record(request.domain, request.dns_token)
    try:
        found = value in (record.strip() for record in await lookup(name))
    except checks.CertificationError as error:
        raise _fail(error) from None
    if not found:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="dns_record_not_found")
    request.domain_verified_at = datetime.now(UTC)
    await db.commit()
    return await _state(db, tenant_id)


@router.post(
    "/request/submit",
    summary="Send the request to Almena's reviewers (admins only)",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`in_review`, `no_request`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`request_incomplete`: legal name, verified domain and logo"
        },
    },
)
async def submit(tenant_id: AdminTenant, db: DbSession) -> CertificationState:
    request = await _latest(db, tenant_id, OPEN)
    if request is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no_request")
    if request.status == "in_review":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="in_review")
    if not (request.legal_name and request.domain_verified_at and request.logo):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="request_incomplete")
    request.status = "in_review"
    request.submitted_at = datetime.now(UTC)
    await db.commit()
    return await _state(db, tenant_id)


@public.get(
    "/{certification_id}/logo",
    summary="Public: the logo of a certification in force",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}}},
)
async def logo(certification_id: uuid.UUID, db: DbSession) -> Response:
    item = await db.scalar(
        select(Certification)
        .options(undefer(Certification.logo))
        .where(Certification.id == certification_id, Certification.status == "approved")
    )
    if item is None or item.logo is None or not item.logo_type:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return Response(
        item.logo, media_type=item.logo_type, headers={"Cache-Control": "public, max-age=300"}
    )
