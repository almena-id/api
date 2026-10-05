"""The value domains Almena's catalogue draws codes from.

A value domain is a list of codes — countries (ISO 3166-1), languages (ISO
639-1), file formats with their media types… — that coded fields take their
values from. They are the trust anchor's (`registry_api.trust_anchor`): every
tenant reads them (`/catalog/fields`) and may build its own fields on them, but
only the anchor's members add, change and delete them (`anchor_only` for any
other tenant). Its key never changes.

What answers, forms and issued credentials hold are a domain's values, so one
that a field draws on only grows: its codes are renamed and new ones added,
none taken away nor any media type changed (`domain_in_use`); one no field
draws on is deleted. `file_format` is the file fields': its codes all carry a
media type, and every file field draws on it.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import field_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import CatalogDomain, CatalogField, Tenant

router = APIRouter(prefix="/tenants/{tenant_id}/value-domains", tags=["fields"])

KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MEDIA_TYPE = re.compile(r"^[a-z]+/[a-z0-9][a-z0-9.+-]*$")
FILE_FORMAT = "file_format"


class CodeIn(BaseModel):
    # A short text or a whole number; one kind for a whole domain.
    value: StrictStr | StrictInt
    labels: dict[str, str]
    # File formats: the media type a file in this format comes as.
    media_type: str | None = Field(default=None, max_length=100)


class DomainIn(BaseModel):
    key: str = Field(max_length=64)
    # In every language of the portal.
    labels: dict[str, str]
    # The standard or list it comes from (ISO 3166-1, …).
    source: str = Field(max_length=200)
    codes: list[CodeIn] = Field(max_length=2000)


class DomainPatch(BaseModel):
    """What changes: only what is sent. The key never does; `codes`, when sent,
    is the whole list in its order."""

    labels: dict[str, str] | None = None
    source: str | None = Field(default=None, max_length=200)
    codes: list[CodeIn] | None = Field(default=None, max_length=2000)


class DomainOut(BaseModel):
    id: uuid.UUID
    key: str
    labels: dict[str, str]
    source: str
    codes: list[dict[str, Any]]
    # How many fields, of any tenant, draw on it.
    uses: int
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


async def _anchor(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    tenant = await db.get_one(Tenant, tenant_id)
    if not tenant.root:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="anchor_only")


def _texts(given: dict[str, str] | None, code: str, limit: int = 200) -> dict[str, str]:
    """In every language of the portal, trimmed."""
    kept = {lang: text.strip() for lang, text in (given or {}).items()}
    if set(kept) != set(catalog.LANGUAGES) or not all(kept.values()):
        raise _refuse(code)
    if any(len(text) > limit for text in kept.values()):
        raise _refuse(code)
    return kept


def _codes(key: str, codes: list[CodeIn]) -> list[dict[str, Any]]:
    """The codes as kept, or why they cannot be: one at least, each value once
    and all of one kind (texts of 100 at most, or whole numbers), named in
    every language; a media type for each file format, and for nothing else."""
    if not codes:
        raise _refuse("codes_invalid")
    kept: list[dict[str, Any]] = []
    for code in codes:
        value = code.value.strip() if isinstance(code.value, str) else code.value
        if value == "" or (isinstance(value, str) and len(value) > 100):
            raise _refuse("codes_invalid")
        entry: dict[str, Any] = {"value": value, "labels": _texts(code.labels, "codes_invalid")}
        media = (code.media_type or "").strip()
        if (key == FILE_FORMAT) != bool(media) or (media and not MEDIA_TYPE.match(media)):
            raise _refuse("media_type_invalid")
        if media:
            entry["media_type"] = media
        kept.append(entry)
    values = [entry["value"] for entry in kept]
    if len({str(value) for value in values}) != len(values):
        raise _refuse("codes_invalid")
    if len({type(value) for value in values}) != 1:
        raise _refuse("codes_invalid")
    return kept


def _draws_on(definition: dict[str, Any], key: str) -> bool:
    """Whether a field (or one of a group's parts) takes its values from `key`."""
    if definition.get("domain") == key or (key == FILE_FORMAT and "formats" in definition):
        return True
    return any(
        _draws_on(part.get("field", {}).get("definition", {}), key)
        for part in definition.get("parts", [])
    )


async def _uses(db: AsyncSession, key: str) -> int:
    definitions: list[dict[str, Any]] = list(await db.scalars(select(CatalogField.definition)))
    return sum(1 for definition in definitions if _draws_on(definition, key))


async def _out(db: AsyncSession, item: CatalogDomain) -> DomainOut:
    return DomainOut(
        id=item.id,
        key=item.key,
        labels=item.labels,
        source=item.source,
        codes=item.codes,
        uses=await _uses(db, item.key),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _owned(db: AsyncSession, tenant_id: uuid.UUID, domain_id: uuid.UUID) -> CatalogDomain:
    await _anchor(db, tenant_id)
    item = await db.get(CatalogDomain, domain_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="domain_not_found")
    return item


@router.get(
    "",
    summary="The trust anchor's value domains, in the catalogue's order",
    responses={status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"}},
)
async def list_domains(tenant_id: TenantId, db: DbSession) -> list[DomainOut]:
    await _anchor(db, tenant_id)
    items = await db.scalars(
        select(CatalogDomain)
        .where(CatalogDomain.tenant_id == tenant_id)
        .order_by(CatalogDomain.created_at, CatalogDomain.key)
    )
    return [await _out(db, item) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a value domain to Almena's catalogue (the trust anchor only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_409_CONFLICT: {"description": "`key_exists`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`key_invalid`, `labels_required`, `source_required`, "
            "`codes_invalid` (none, repeated, of two kinds or not named in every "
            "language), `media_type_invalid` (one for each file format, none elsewhere)"
        },
    },
)
async def create_domain(tenant_id: TenantId, body: DomainIn, db: DbSession) -> DomainOut:
    await _anchor(db, tenant_id)
    if not KEY.match(body.key):
        raise _refuse("key_invalid")
    labels = _texts(body.labels, "labels_required")
    source = body.source.strip()
    if not source:
        raise _refuse("source_required")
    codes = _codes(body.key, body.codes)
    exists = await db.scalar(
        select(CatalogDomain.id).where(
            CatalogDomain.tenant_id == tenant_id, CatalogDomain.key == body.key
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="key_exists")
    item = CatalogDomain(
        tenant_id=tenant_id,
        key=body.key,
        labels=labels,
        source=source,
        codes=codes,
        # Set here, to the microsecond: the catalogue is listed in this order.
        created_at=datetime.now(UTC),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.patch(
    "/{domain_id}",
    summary="Change a value domain (the trust anchor only); never its key",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_404_NOT_FOUND: {"description": "`domain_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`domain_in_use`: a field draws on it, and the change would "
            "take a code away, change one's value kind or its media type"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "as when adding it"},
    },
)
async def update_domain(
    tenant_id: TenantId, domain_id: uuid.UUID, body: DomainPatch, db: DbSession
) -> DomainOut:
    item = await _owned(db, tenant_id, domain_id)
    sent = body.model_fields_set
    if "labels" in sent:
        item.labels = _texts(body.labels, "labels_required")
    if "source" in sent:
        source = (body.source or "").strip()
        if not source:
            raise _refuse("source_required")
        item.source = source
    if "codes" in sent:
        codes = _codes(item.key, body.codes or [])
        before = {code["value"]: code.get("media_type") for code in item.codes}
        after = {code["value"]: code.get("media_type") for code in codes}
        kept = all(value in after and after[value] == media for value, media in before.items())
        if not kept and await _uses(db, item.key):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="domain_in_use")
        item.codes = codes
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.delete(
    "/{domain_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a value domain no field draws on (the trust anchor only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_404_NOT_FOUND: {"description": "`domain_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`domain_in_use`: a field draws on it"},
    },
)
async def delete_domain(tenant_id: TenantId, domain_id: uuid.UUID, db: DbSession) -> Response:
    item = await _owned(db, tenant_id, domain_id)
    if item.key == FILE_FORMAT or await _uses(db, item.key):
        # File fields always draw on the file formats.
        raise HTTPException(status.HTTP_409_CONFLICT, detail="domain_in_use")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
