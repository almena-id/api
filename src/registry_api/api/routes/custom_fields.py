"""The fields a tenant adds to Almena's catalogue for its own forms.

Almena's catalogue (`registry_api.field_catalog`) comes first; when it lacks
what a tenant needs, the tenant defines a field of its own: a key (never one of
Almena's ids), a type, its labels in the portal's languages and what the type
needs — a length and a pattern for text, the options of a list, the formats of
a file. Forms refer to it as `custom:{key}`. Custom fields are the tenant's
alone: they are not published on the identity domain and no other tenant sees
them. Any member creates them; one that a form uses cannot be deleted.
"""

import re
import uuid
from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import field_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import CustomField, Form

router = APIRouter(prefix="/tenants/{tenant_id}/fields", tags=["fields"])

PREFIX = "custom:"
KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CustomType = Literal["text", "email", "phone", "date", "code", "codes", "file"]
# What each type takes; any other is refused rather than ignored.
TAKES: dict[str, set[str]] = {
    "text": {"max_length", "pattern"},
    "code": {"options"},
    "codes": {"options"},
    "file": {"formats"},
}


class OptionIn(BaseModel):
    value: str = Field(min_length=1, max_length=100)
    labels: dict[str, str]


class CustomFieldIn(BaseModel):
    key: str = Field(max_length=64)
    type: CustomType
    # Per language of the portal (`en`, `es`); at least one.
    labels: dict[str, str]
    max_length: int | None = Field(default=None, ge=1, le=10000)
    pattern: str | None = Field(default=None, max_length=500)
    options: list[OptionIn] | None = Field(default=None, max_length=300)
    formats: list[str] | None = Field(default=None, max_length=50)


class CustomFieldOut(BaseModel):
    id: uuid.UUID
    slug: str
    key: str
    # How forms refer to it: `custom:{key}`.
    ref: str
    # The field in the catalogue's own shape, its values inline.
    field: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


def _labels(labels: dict[str, str], code: str) -> dict[str, str]:
    """The labels in the portal's languages, trimmed; at least one."""
    if set(labels) - set(catalog.LANGUAGES):
        raise _refuse(code)
    kept = {lang: text.strip() for lang, text in labels.items() if text.strip()}
    if not kept or any(len(text) > 200 for text in kept.values()):
        raise _refuse(code)
    return kept


def _definition(body: CustomFieldIn) -> dict[str, Any]:
    given = body.model_dump(include={"max_length", "pattern", "options", "formats"})
    given = {key: value for key, value in given.items() if value not in (None, "")}
    if set(given) - TAKES.get(body.type, set()):
        raise _refuse("constraint_invalid")
    if "pattern" in given:
        pattern = given["pattern"].strip()
        try:
            re.compile(pattern)
        except re.error:
            raise _refuse("pattern_invalid") from None
        given["pattern"] = pattern
    if body.type in ("code", "codes"):
        options = [
            {"value": option.value.strip(), "labels": _labels(option.labels, "options_invalid")}
            for option in body.options or []
        ]
        values = [option["value"] for option in options]
        if len(values) < 2 or not all(values) or len(set(values)) != len(values):
            raise _refuse("options_invalid")
        given["options"] = options
    if body.type == "file":
        known = catalog.domain_values(catalog.BY_ID["document_file"])
        formats = body.formats or []
        if not formats or len(set(formats)) != len(formats) or set(formats) - set(known):
            raise _refuse("formats_invalid")
        # In the catalogue's order.
        given["formats"] = [value for value in known if value in formats]
    return given


def as_catalog_field(item: CustomField) -> catalog.Field:
    """The tenant's field as the catalogue's fields are, for forms."""
    definition = item.definition
    codes = tuple(
        catalog.Code(option["value"], option["labels"]) for option in definition.get("options", [])
    )
    return catalog.Field(
        PREFIX + item.key,
        cast(catalog.FieldType, item.type),
        item.labels,
        "custom",
        "custom",
        max_length=definition.get("max_length"),
        pattern=definition.get("pattern"),
        domain="file_format" if item.type == "file" else None,
        extra={"values": tuple(definition["formats"])} if "formats" in definition else {},
        codes=codes,
        published=False,
    )


async def custom_catalog(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, catalog.Field]:
    """The tenant's own fields, by the ref forms use."""
    items = await db.scalars(select(CustomField).where(CustomField.tenant_id == tenant_id))
    return {PREFIX + item.key: as_catalog_field(item) for item in items}


def _out(item: CustomField) -> CustomFieldOut:
    return CustomFieldOut(
        id=item.id,
        slug=item.slug,
        key=item.key,
        ref=PREFIX + item.key,
        field=catalog.field_out(as_catalog_field(item)),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", summary="The tenant's own fields, newest first")
async def list_fields(tenant_id: TenantId, db: DbSession) -> list[CustomFieldOut]:
    items = await db.scalars(
        select(CustomField)
        .where(CustomField.tenant_id == tenant_id)
        .order_by(CustomField.created_at.desc(), CustomField.id.desc())
    )
    return [_out(item) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a field of the tenant's own to its catalogue",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`key_exists`: the tenant has it already"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`key_invalid`, `key_reserved` (one of Almena's), "
            "`labels_required`, `constraint_invalid`, `pattern_invalid`, "
            "`options_invalid` or `formats_invalid`"
        },
    },
)
async def create_field(tenant_id: TenantId, body: CustomFieldIn, db: DbSession) -> CustomFieldOut:
    if not KEY.match(body.key):
        raise _refuse("key_invalid")
    if body.key in catalog.BY_ID:
        raise _refuse("key_reserved")
    labels = _labels(body.labels, "labels_required")
    definition = _definition(body)
    exists = await db.scalar(
        select(CustomField.id).where(
            CustomField.tenant_id == tenant_id, CustomField.key == body.key
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="key_exists")
    item = CustomField(
        tenant_id=tenant_id, key=body.key, type=body.type, labels=labels, definition=definition
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _out(item)


@router.delete(
    "/{field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of the tenant's own fields, unless a form uses it",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`field_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`field_in_use`: a form asks for it"},
    },
)
async def delete_field(tenant_id: TenantId, field_id: uuid.UUID, db: DbSession) -> Response:
    item = await db.get(CustomField, field_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="field_not_found")
    ref = PREFIX + item.key
    forms: list[list[dict[str, Any]]] = list(
        await db.scalars(select(Form.fields).where(Form.tenant_id == tenant_id))
    )
    if any(field.get("ref") == ref for fields in forms for field in fields):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="field_in_use")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
