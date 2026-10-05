"""The fields a tenant keeps in its catalogue for its forms.

Almena's catalogue — the trust anchor's fields (`registry_api.trust_anchor`) —
comes first; when it lacks what a tenant needs, the tenant defines a field of
its own: a key (never one of the anchor's), a type, its labels in the portal's
languages and what the type needs — a length and a pattern for text, the
options of a list, the formats of a file. Forms refer to it as `custom:{key}`.
Custom fields are the tenant's alone: they are not published on the identity
domain and no other tenant sees them. Any member creates and changes them while
the tenant's subscription gives `own_fields` (`registry_api.entitlements`;
`subscription_required` otherwise); deleting needs none, and one that a form
uses cannot be deleted.

The anchor keeps its fields here too, and they are everyone's: referred to by
their key, published, each with its category and the standard it is named
after (`source`), labelled in every language of the portal. One that any
tenant's form or one of its credential types uses cannot be deleted.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import entitlements, trust_anchor
from registry_api import field_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import CatalogField, CatalogType, Form, Tenant
from registry_api.trust_anchor import PREFIX

router = APIRouter(prefix="/tenants/{tenant_id}/fields", tags=["fields"])

KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CustomType = Literal["text", "email", "phone", "date", "code", "codes", "file", "group"]
# What a group's parts may be: anything but files and groups.
PartType = Literal["text", "email", "phone", "date", "code", "codes"]
# What each type takes; any other is refused rather than ignored.
TAKES: dict[str, set[str]] = {
    "text": {"max_length", "pattern"},
    # Its own options, or one of the anchor's value domains.
    "code": {"options", "domain"},
    "codes": {"options", "domain"},
    "file": {"formats"},
    # The trust anchor's only: fields that only make sense together.
    "group": {"parts"},
}


class OptionIn(BaseModel):
    value: str = Field(min_length=1, max_length=100)
    labels: dict[str, str]


class PartIn(BaseModel):
    """One part of a group: a field of its own, named by a key in the group."""

    key: str = Field(max_length=64)
    required: bool = True
    type: PartType
    # In every language of the portal.
    labels: dict[str, str]
    source: str = Field(default="", max_length=200)
    max_length: int | None = Field(default=None, ge=1, le=10000)
    pattern: str | None = Field(default=None, max_length=500)
    options: list[OptionIn] | None = Field(default=None, max_length=300)
    domain: str | None = Field(default=None, max_length=64)


class CustomFieldIn(BaseModel):
    key: str = Field(max_length=64)
    type: CustomType
    # Per language of the portal (`en`, `es`); at least one.
    labels: dict[str, str]
    max_length: int | None = Field(default=None, ge=1, le=10000)
    pattern: str | None = Field(default=None, max_length=500)
    options: list[OptionIn] | None = Field(default=None, max_length=300)
    formats: list[str] | None = Field(default=None, max_length=50)
    # `code` and `codes`: one of the anchor's value domains, in place of options.
    domain: str | None = Field(default=None, max_length=64)
    # `group` (the trust anchor's only): its parts, in order.
    parts: list[PartIn] | None = Field(default=None, max_length=20)
    # The anchor's fields only: one of its field categories, and the standard
    # the field is named after.
    category: str | None = Field(default=None, max_length=32)
    source: str | None = Field(default=None, max_length=200)


class CustomFieldPatch(BaseModel):
    """What changes: only what is sent. The key never does. `type`,
    `max_length`, `pattern`, `options` and `formats` go together: sending any
    of them sets the field's definition to what is sent."""

    type: CustomType | None = None
    labels: dict[str, str] | None = None
    max_length: int | None = Field(default=None, ge=1, le=10000)
    pattern: str | None = Field(default=None, max_length=500)
    options: list[OptionIn] | None = Field(default=None, max_length=300)
    formats: list[str] | None = Field(default=None, max_length=50)
    domain: str | None = Field(default=None, max_length=64)
    parts: list[PartIn] | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=32)
    source: str | None = Field(default=None, max_length=200)


DEFINED = {"type", "max_length", "pattern", "options", "formats", "domain", "parts"}


class CustomFieldOut(BaseModel):
    id: uuid.UUID
    slug: str
    key: str
    # How forms refer to it: `custom:{key}`; the anchor's, by its key.
    ref: str
    # The field in the catalogue's own shape, its values inline.
    field: dict[str, Any]
    # As kept, for changing it: `max_length`, `pattern`, `options`, `formats`,
    # `domain`, `values`, `repeatable`, `parts`.
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


def _labels(labels: dict[str, str], code: str, every: bool = False) -> dict[str, str]:
    """The labels in the portal's languages, trimmed; at least one, or `every`
    one of them."""
    if set(labels) - set(catalog.LANGUAGES):
        raise _refuse(code)
    kept = {lang: text.strip() for lang, text in labels.items() if text.strip()}
    if not kept or any(len(text) > 200 for text in kept.values()):
        raise _refuse(code)
    if every and set(kept) != set(catalog.LANGUAGES):
        raise _refuse(code)
    return kept


def _parts(parts: list[PartIn], found: trust_anchor.Catalogue) -> list[dict[str, Any]]:
    """A group's parts as kept, or why they cannot be: one at least, each key
    once, labelled in every language, each a field of its own."""
    keys = [part.key for part in parts]
    if not parts or len(set(keys)) != len(keys) or not all(KEY.match(key) for key in keys):
        raise _refuse("parts_invalid")
    kept = []
    for part in parts:
        kept.append(
            {
                "key": part.key,
                "required": part.required,
                "field": {
                    "type": part.type,
                    "labels": _labels(part.labels, "parts_invalid", every=True),
                    "source": part.source.strip(),
                    "definition": _definition(part, part.type, found),
                },
            }
        )
    return kept


def _definition(
    body: CustomFieldIn | CustomFieldPatch | PartIn, kind: str, found: trust_anchor.Catalogue
) -> dict[str, Any]:
    """The definition `body` gives a field of type `kind`, or why it cannot."""
    given = body.model_dump(
        include={"max_length", "pattern", "options", "formats", "domain", "parts"}
    )
    given = {key: value for key, value in given.items() if value not in (None, "")}
    if set(given) - TAKES.get(kind, set()):
        raise _refuse("constraint_invalid")
    if "pattern" in given:
        pattern = given["pattern"].strip()
        try:
            re.compile(pattern)
        except re.error:
            raise _refuse("pattern_invalid") from None
        given["pattern"] = pattern
    if kind in ("code", "codes") and "domain" in given:
        # One of the anchor's value domains, whole; file formats are a file's.
        if "options" in given or given["domain"] not in found.domains:
            raise _refuse("domain_invalid")
        if given["domain"] == "file_format":
            raise _refuse("domain_invalid")
    elif kind in ("code", "codes"):
        options = [
            {"value": option.value.strip(), "labels": _labels(option.labels, "options_invalid")}
            for option in body.options or []
        ]
        values = [option["value"] for option in options]
        if len(values) < 2 or not all(values) or len(set(values)) != len(values):
            raise _refuse("options_invalid")
        given["options"] = options
    if kind == "group":
        given["parts"] = _parts(getattr(body, "parts", None) or [], found)
    if kind == "file":
        domain = found.domains.get("file_format")
        known = [code.value for code in domain.codes] if domain else []
        formats = getattr(body, "formats", None) or []
        if not formats or len(set(formats)) != len(formats) or set(formats) - set(known):
            raise _refuse("formats_invalid")
        # In the catalogue's order.
        given["formats"] = [value for value in known if value in formats]
    return given


def _out(item: CatalogField, domains: dict[str, catalog.Domain], anchor: bool) -> CustomFieldOut:
    field = trust_anchor.as_field(item, domains, anchor)
    return CustomFieldOut(
        id=item.id,
        slug=item.slug,
        key=item.key,
        ref=field.id,
        field=catalog.field_out(field),
        definition=item.definition,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", summary="The tenant's own fields (the anchor's: everyone's), newest first")
async def list_fields(tenant_id: TenantId, db: DbSession) -> list[CustomFieldOut]:
    anchor = tenant_id == await trust_anchor.anchor_id(db)
    domains = (await trust_anchor.load(db)).domains
    items = await db.scalars(
        select(CatalogField)
        .where(CatalogField.tenant_id == tenant_id)
        .order_by(CatalogField.created_at.desc(), CatalogField.id.desc())
    )
    return [_out(item, domains, anchor) for item in items]


async def _entitled(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Making and changing fields of one's own takes a subscription (the anchor
    always may); deleting them does not."""
    tenant = await db.get_one(Tenant, tenant_id)
    if not await entitlements.allows(db, tenant, "own_fields"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="subscription_required")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a field of the tenant's own to its catalogue",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`subscription_required`"},
        status.HTTP_409_CONFLICT: {"description": "`key_exists`: the tenant has it already"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`key_invalid`, `key_reserved` (one of the anchor's), "
            "`labels_required` (the anchor's: every language), `constraint_invalid`, "
            "`pattern_invalid`, `options_invalid`, `formats_invalid`, `domain_invalid` (not "
            "one of the anchor's value domains, or with options), `parts_invalid` (a "
            "group's: none, a key twice, not labelled in every language), `category_invalid` "
            "or `source_required` (the anchor's), or `anchor_only` (a category, a "
            "source or a group from another tenant)"
        },
    },
)
async def create_field(tenant_id: TenantId, body: CustomFieldIn, db: DbSession) -> CustomFieldOut:
    await _entitled(db, tenant_id)
    anchor = tenant_id == await trust_anchor.anchor_id(db)
    found = await trust_anchor.load(db)
    if not KEY.match(body.key):
        raise _refuse("key_invalid")
    if not anchor and body.key in found.fields:
        raise _refuse("key_reserved")
    if body.type == "group" and not anchor:
        raise _refuse("anchor_only")
    labels = _labels(body.labels, "labels_required", every=anchor)
    category = source = ""
    if anchor:
        category = (body.category or "").strip()
        if category not in found.field_categories:
            raise _refuse("category_invalid")
        source = (body.source or "").strip()
        if not source:
            raise _refuse("source_required")
    elif body.category is not None or body.source is not None:
        raise _refuse("anchor_only")
    definition = _definition(body, body.type, found)
    exists = await db.scalar(
        select(CatalogField.id).where(
            CatalogField.tenant_id == tenant_id, CatalogField.key == body.key
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="key_exists")
    item = CatalogField(
        tenant_id=tenant_id,
        key=body.key,
        type=body.type,
        labels=labels,
        source=source,
        category=category,
        definition=definition,
        # Set here, to the microsecond: the catalogue is listed in this order.
        created_at=datetime.now(UTC),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _out(item, found.domains, anchor)


async def _in_use(db: AsyncSession, item: CatalogField, anchor: bool) -> bool:
    """Whether a form or a credential type asks for it — the anchor's: any
    tenant's form or type; a tenant's own: its forms and types."""
    ref = item.key if anchor else PREFIX + item.key
    forms_query = select(Form.fields)
    types_query = select(CatalogType.claims)
    if not anchor:
        forms_query = forms_query.where(Form.tenant_id == item.tenant_id)
        types_query = types_query.where(CatalogType.tenant_id == item.tenant_id)
    forms: list[list[dict[str, Any]]] = list(await db.scalars(forms_query))
    if any(field.get("ref") == ref for fields in forms for field in fields):
        return True
    claims: list[list[dict[str, Any]]] = list(await db.scalars(types_query))
    return any(claim["field"] == ref for kept in claims for claim in kept)


def _source(definition: dict[str, Any]) -> tuple[str | None, set[str] | None]:
    """Where a coded or file field's values come from — a value domain, or its
    own options — and which of them it takes (`None`: all of the domain)."""
    if "formats" in definition:
        return "file_format", {str(value) for value in definition["formats"]}
    if "options" in definition:
        return None, {str(option["value"]) for option in definition["options"]}
    values = definition.get("values")
    return definition.get("domain"), {str(value) for value in values} if values else None


def _group_looser(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Whether a group `new` takes every answer `old` took: every part still
    there, of the same type, taking all it took and required no more than it
    was; new parts optional."""
    before = {part["key"]: part for part in old.get("parts", [])}
    after = {part["key"]: part for part in new.get("parts", [])}
    for key, part in before.items():
        now = after.get(key)
        if now is None or now["field"]["type"] != part["field"]["type"]:
            return False
        if now.get("required", True) and not part.get("required", True):
            return False
        if not _looser(part["field"].get("definition", {}), now["field"].get("definition", {})):
            return False
    return not any(part.get("required", True) for key, part in after.items() if key not in before)


def _looser(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Whether `new` takes every answer `old` took: no shorter length, no new
    pattern, values from the same place and every one still taken."""
    if "parts" in old or "parts" in new:
        return "parts" in old and "parts" in new and _group_looser(old, new)
    length = new.get("max_length")
    if length is not None and length < old.get("max_length", length + 1):
        return False
    if new.get("pattern") and new["pattern"] != old.get("pattern"):
        return False
    (was, before), (now, after) = _source(old), _source(new)
    if was != now:
        return False
    if after is None or before is None:
        return after is None
    return before <= after


async def _owned(db: AsyncSession, tenant_id: uuid.UUID, field_id: uuid.UUID) -> CatalogField:
    item = await db.get(CatalogField, field_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="field_not_found")
    return item


@router.patch(
    "/{field_id}",
    summary="Change one of the tenant's own fields (the anchor's: everyone's); never its key",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`subscription_required`"},
        status.HTTP_404_NOT_FOUND: {"description": "`field_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`field_in_use`: something uses it, and the change would turn "
            "away what it took — another type, a shorter length, another pattern, an "
            "option or a format less; a group's part taken away, changed so, or made "
            "required, or a new part required"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "as when adding it"},
    },
)
async def update_field(
    tenant_id: TenantId, field_id: uuid.UUID, body: CustomFieldPatch, db: DbSession
) -> CustomFieldOut:
    item = await _owned(db, tenant_id, field_id)
    await _entitled(db, tenant_id)
    anchor = tenant_id == await trust_anchor.anchor_id(db)
    found = await trust_anchor.load(db)
    sent = body.model_fields_set
    if "labels" in sent:
        item.labels = _labels(body.labels or {}, "labels_required", every=anchor)
    if not anchor and ({"category", "source"} & sent):
        raise _refuse("anchor_only")
    if "category" in sent:
        category = (body.category or "").strip()
        if category not in found.field_categories:
            raise _refuse("category_invalid")
        item.category = category
    if "source" in sent:
        source = (body.source or "").strip()
        if not source:
            raise _refuse("source_required")
        item.source = source
    if DEFINED & sent:
        kind = body.type or item.type
        if kind == "group" and not anchor:
            raise _refuse("anchor_only")
        definition = _definition(body, kind, found)
        if item.definition.get("repeatable"):
            definition["repeatable"] = True
        if (kind != item.type or not _looser(item.definition, definition)) and await _in_use(
            db, item, anchor
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="field_in_use")
        item.type, item.definition = kind, definition
    await db.commit()
    await db.refresh(item)
    return _out(item, found.domains, anchor)


@router.delete(
    "/{field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of the tenant's own fields, unless a form uses it",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`field_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`field_in_use`: a form or a credential type asks for it "
            "(the anchor's: any tenant's)"
        },
    },
)
async def delete_field(tenant_id: TenantId, field_id: uuid.UUID, db: DbSession) -> Response:
    item = await _owned(db, tenant_id, field_id)
    if await _in_use(db, item, tenant_id == await trust_anchor.anchor_id(db)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="field_in_use")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
