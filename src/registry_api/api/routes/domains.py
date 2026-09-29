"""The domains a tenant links to itself.

Admins add a domain and get the DNS TXT record that proves it
(``_almena.{domain}`` = ``almena-verify=…``); once the record is found the
domain is verified and the tenant's DID document names it (`LinkedDomains`),
which leaves its identity with changes to sign. Members see them. Removing a
domain takes it out of the document the same way.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry_api import dns_proof as checks
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.models import TenantDomain

router = APIRouter(prefix="/tenants/{tenant_id}/domains", tags=["domains"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]
TxtLookup = Annotated[checks.TxtLookup, Depends(checks.get_txt_lookup)]


class DnsRecord(BaseModel):
    type: str = "TXT"
    name: str
    value: str


class DomainOut(BaseModel):
    id: uuid.UUID
    domain: str
    # What to publish in the DNS to prove it.
    dns_record: DnsRecord
    verified: bool
    verified_at: datetime | None
    created_at: datetime


class DomainIn(BaseModel):
    # Whatever was typed: `https://Acme.com/about` is `acme.com`.
    domain: str = Field(min_length=1, max_length=300)


def _out(item: TenantDomain) -> DomainOut:
    name, value = checks.dns_record(item.domain, item.dns_token)
    return DomainOut(
        id=item.id,
        domain=item.domain,
        dns_record=DnsRecord(name=name, value=value),
        verified=item.verified_at is not None,
        verified_at=item.verified_at,
        created_at=item.created_at,
    )


async def _owned(db: DbSession, tenant_id: uuid.UUID, domain_id: uuid.UUID) -> TenantDomain:
    item = await db.get(TenantDomain, domain_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="domain_not_found")
    return item


@router.get("", summary="The tenant's domains, oldest first")
async def list_domains(tenant_id: TenantId, db: DbSession) -> list[DomainOut]:
    items = await db.scalars(
        select(TenantDomain)
        .where(TenantDomain.tenant_id == tenant_id)
        .order_by(TenantDomain.created_at, TenantDomain.id)
    )
    return [_out(item) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a domain (admins): it comes with the DNS record that proves it",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`domain_exists`: already linked"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "`domain_invalid`"},
    },
)
async def add_domain(tenant_id: AdminTenant, body: DomainIn, db: DbSession) -> DomainOut:
    try:
        domain = checks.domain(body.domain)
    except checks.DomainError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code) from None
    exists = await db.scalar(
        select(TenantDomain.id).where(
            TenantDomain.tenant_id == tenant_id, TenantDomain.domain == domain
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="domain_exists")
    item = TenantDomain(
        tenant_id=tenant_id,
        domain=domain,
        dns_token=checks.new_token(),
        created_at=datetime.now(UTC),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _out(item)


@router.post(
    "/{domain_id}/check",
    summary="Look for the DNS record (admins): found, the domain is verified",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`domain_not_found`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "`dns_record_not_found`"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "`dns_unavailable`"},
    },
)
async def check_domain(
    tenant_id: AdminTenant, domain_id: uuid.UUID, db: DbSession, lookup: TxtLookup
) -> DomainOut:
    item = await _owned(db, tenant_id, domain_id)
    name, value = checks.dns_record(item.domain, item.dns_token)
    try:
        found = value in (record.strip() for record in await lookup(name))
    except checks.DomainError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.code) from None
    if not found:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="dns_record_not_found")
    if item.verified_at is None:
        item.verified_at = datetime.now(UTC)
        await db.commit()
    return _out(item)


@router.delete(
    "/{domain_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a domain (admins)",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`domain_not_found`"}},
)
async def remove_domain(tenant_id: AdminTenant, domain_id: uuid.UUID, db: DbSession) -> Response:
    await db.delete(await _owned(db, tenant_id, domain_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
