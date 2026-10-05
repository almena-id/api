"""Credential types: Almena's, which the trust anchor keeps for everyone, and
a tenant's own.

Almena's credential catalogue is the trust anchor's (`registry_api.trust_anchor`):
its members add types to it here, and every tenant's issuers grant them and
forms ask for them (`/catalog/credentials` publishes them). A type's claims are
the anchor's fields; its key names its `vct` (``{did_url}/credentials/{key}/v1``)
unless it is issued under a framework of its own (`issuance: external`, the
EU PID), which names its own `vct` and is only asked for.

A tenant whose subscription allows it (`registry_api.entitlements`;
`subscription_required` otherwise) keeps types of its own, as `custom:{key}`:
only its issuers grant them and only its forms ask for them. Their claims are
the anchor's fields or its own; its issuers issue them, under a `vct` of the
identity domain in the tenant's name (``{did_url}/credentials/{slug}/{key}/v1``),
whose type metadata and schema are published there like Almena's — the
credentials leave the platform. Labels in one language at least; no standard
needed.

A type an issuer grants, a form asks for or an application was made for is not
deleted, and changes only in ways that keep what was issued valid.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import credential_catalog as credentials
from registry_api import entitlements, trust_anchor
from registry_api import field_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import Application, CatalogType, Form, Issuer, Tenant
from registry_api.trust_anchor import PREFIX

router = APIRouter(prefix="/tenants/{tenant_id}/credential-types", tags=["fields"])

KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
W3C_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]{0,99}$")
DOCTYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
# A URI naming a type elsewhere: a scheme, then no spaces.
VCT = re.compile(r"^[a-z][a-z0-9+.-]*:\S+$")


class ClaimIn(BaseModel):
    # One of the anchor's fields, or — a tenant's own type — one of its own
    # (`custom:{key}`).
    field: str = Field(max_length=64)
    required: bool = True


class CredentialTypeIn(BaseModel):
    key: str = Field(max_length=64)
    # Per language of the portal: the anchor's in every one, a tenant's in one
    # at least.
    labels: dict[str, str]
    descriptions: dict[str, str]
    # One of the anchor's credential categories.
    category: str = Field(max_length=32)
    # The standard or model it follows; a tenant's own may follow none.
    source: str = Field(default="", max_length=200)
    claims: list[ClaimIn] = Field(max_length=50)
    issuance: Literal["almena", "external"] = "almena"
    # `external` (the anchor's only): how its framework names it.
    vct: str | None = Field(default=None, max_length=500)
    w3c_type: str | None = Field(default=None, max_length=100)
    mdoc_doctype: str | None = Field(default=None, max_length=200)


class CredentialTypePatch(BaseModel):
    """What changes: only what is sent. The key never does."""

    labels: dict[str, str] | None = None
    descriptions: dict[str, str] | None = None
    category: str | None = Field(default=None, max_length=32)
    source: str | None = Field(default=None, max_length=200)
    claims: list[ClaimIn] | None = Field(default=None, max_length=50)
    issuance: Literal["almena", "external"] | None = None
    vct: str | None = Field(default=None, max_length=500)
    w3c_type: str | None = Field(default=None, max_length=100)
    mdoc_doctype: str | None = Field(default=None, max_length=200)


# What a type in use keeps: how formats name it and who issues it.
NAMING = ("issuance", "vct", "w3c_type", "mdoc_doctype")


class CredentialTypeOut(BaseModel):
    id: uuid.UUID
    slug: str
    key: str
    # How issuers and forms refer to it: the anchor's by key, a tenant's
    # `custom:{key}`.
    ref: str
    # The type as the catalogue serves it (`/catalog/credentials`).
    type: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


class Owner:
    """The tenant whose types these are, and the catalogue as it sees it."""

    def __init__(self, tenant: Tenant, found: trust_anchor.Catalogue) -> None:
        self.tenant = tenant
        self.anchor = tenant.root
        self.found = found

    @property
    def namespace(self) -> str | None:
        return None if self.anchor else self.tenant.slug

    def ref(self, key: str) -> str:
        return key if self.anchor else PREFIX + key


async def _owner(db: AsyncSession, tenant_id: uuid.UUID, writes: bool = True) -> Owner:
    """The tenant, if it may keep types of its own (the anchor always may)."""
    tenant = await db.get_one(Tenant, tenant_id)
    if writes and not await entitlements.allows(db, tenant, "own_credential_types"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="subscription_required")
    return Owner(tenant, await trust_anchor.load(db, tenant_id))


def _texts(given: dict[str, str], limit: int, code: str, every: bool) -> dict[str, str]:
    """Texts in the portal's languages, trimmed: one at least, or `every` one."""
    if set(given) - set(catalog.LANGUAGES):
        raise _refuse(code)
    kept = {lang: text.strip() for lang, text in given.items() if text.strip()}
    if not kept or (every and set(kept) != set(catalog.LANGUAGES)):
        raise _refuse(code)
    if any(len(text) > limit for text in kept.values()):
        raise _refuse(code)
    return kept


def _out(item: CatalogType, owner: Owner) -> CredentialTypeOut:
    kind = credentials.type_from(trust_anchor.type_data(item), owner.namespace)
    return CredentialTypeOut(
        id=item.id,
        slug=item.slug,
        key=item.key,
        ref=kind.id,
        type=credentials.type_out(kind),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _checked(body: CredentialTypeIn, owner: Owner) -> dict[str, Any]:
    """What a type is kept as, but its key — or why it cannot be. Its W3C type
    is no other type's the tenant sees."""
    found, anchor = owner.found, owner.anchor
    labels = _texts(body.labels, 200, "labels_required", every=anchor)
    descriptions = _texts(body.descriptions, 500, "descriptions_required", every=anchor)
    if body.category not in found.type_categories:
        raise _refuse("category_invalid")
    source = body.source.strip()
    if anchor and not source:
        raise _refuse("source_required")
    fields = [claim.field for claim in body.claims]
    names = [credentials.Claim(field).name for field in fields]
    if (
        not fields
        or len(set(names)) != len(names)
        or any(
            field not in found.fields
            # The anchor's types carry the anchor's fields only.
            or (anchor and not found.fields[field].published)
            # Credentials carry no files.
            or found.fields[field].type == "file"
            for field in fields
        )
    ):
        raise _refuse("claims_invalid")
    if not anchor and body.issuance != "almena":
        # A tenant's own types are its issuers'.
        raise _refuse("issuance_invalid")
    vct = (body.vct or "").strip() or None
    if (body.issuance == "external") != (vct is not None) or (vct and not VCT.match(vct)):
        raise _refuse("vct_invalid")
    w3c_type = (body.w3c_type or "").strip() or None
    if w3c_type and (
        not W3C_TYPE.match(w3c_type)
        or w3c_type == "VerifiableCredential"
        or any(
            item.w3c_type == w3c_type and item.id != owner.ref(body.key)
            for item in found.types.values()
        )
    ):
        raise _refuse("w3c_type_invalid")
    doctype = (body.mdoc_doctype or "").strip() or None
    if doctype and not DOCTYPE.match(doctype):
        raise _refuse("mdoc_doctype_invalid")
    return {
        "labels": labels,
        "descriptions": descriptions,
        "category": body.category,
        "source": source,
        "claims": [claim.model_dump() for claim in body.claims],
        "issuance": body.issuance,
        "vct": vct,
        "w3c_type": w3c_type,
        "mdoc_doctype": doctype,
    }


@router.get("", summary="The credential types the tenant keeps (the anchor's: everyone's)")
async def list_types(tenant_id: TenantId, db: DbSession) -> list[CredentialTypeOut]:
    owner = await _owner(db, tenant_id, writes=False)
    items = await db.scalars(
        select(CatalogType)
        .where(CatalogType.tenant_id == tenant_id)
        .order_by(CatalogType.created_at.desc(), CatalogType.id.desc())
    )
    return [_out(item, owner) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a credential type: to Almena's catalogue (the anchor), or of the tenant's own",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "`subscription_required`: the tenant's subscription does not "
            "let it keep types of its own"
        },
        status.HTTP_409_CONFLICT: {"description": "`key_exists`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`key_invalid`, `labels_required`, `descriptions_required` (every "
            "language), `category_invalid`, `source_required`, `claims_invalid` (none, "
            "repeated or not the anchor's fields), `vct_invalid` (required for `external` "
            "types, refused for the anchor's own), `w3c_type_invalid` (or taken) or "
            "`mdoc_doctype_invalid`"
        },
    },
)
async def create_type(
    tenant_id: TenantId, body: CredentialTypeIn, db: DbSession
) -> CredentialTypeOut:
    owner = await _owner(db, tenant_id)
    if not KEY.match(body.key):
        raise _refuse("key_invalid")
    values = _checked(body, owner)
    if owner.ref(body.key) in owner.found.types:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="key_exists")
    item = CatalogType(
        tenant_id=tenant_id,
        key=body.key,
        **values,
        # Set here, to the microsecond: the catalogue is listed in this order.
        created_at=datetime.now(UTC),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _out(item, owner)


async def _in_use(db: AsyncSession, item: CatalogType, owner: Owner) -> bool:
    """Whether an issuer grants it, a form asks for it or an application was
    made for it — a tenant's own: its issuers, forms and applications."""
    ref = owner.ref(item.key)
    granted_query = select(Issuer.credential_types)
    asked_query = select(Form.credentials)
    applied_query = select(Application.id).where(Application.credential_type == ref)
    if not owner.anchor:
        granted_query = granted_query.where(Issuer.tenant_id == item.tenant_id)
        asked_query = asked_query.where(Form.tenant_id == item.tenant_id)
        applied_query = applied_query.join(Issuer, Issuer.id == Application.issuer_id).where(
            Issuer.tenant_id == item.tenant_id
        )
    granted: list[list[str]] = list(await db.scalars(granted_query))
    asked: list[list[dict[str, Any]]] = list(await db.scalars(asked_query))
    applied = await db.scalar(applied_query.limit(1))
    return (
        any(ref in (types or []) for types in granted)
        or any(entry.get("type") == ref for entries in asked for entry in entries or [])
        or applied is not None
    )


async def _owned(
    db: AsyncSession, tenant_id: uuid.UUID, type_id: uuid.UUID, writes: bool = True
) -> tuple[CatalogType, Owner]:
    owner = await _owner(db, tenant_id, writes)
    item = await db.get(CatalogType, type_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return item, owner


def _kept_claims(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> bool:
    """Whether a type in use may carry `new`: every claim it had, none made
    always-present that was not, and new ones only optional."""
    before = {claim["field"]: claim.get("required", True) for claim in old}
    after = {claim["field"]: claim.get("required", True) for claim in new}
    if set(before) - set(after):
        return False
    return all(not required or before.get(field, False) for field, required in after.items())


@router.patch(
    "/{type_id}",
    summary="Change one of the tenant's credential types (the anchor's: Almena's); never its key",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "`subscription_required`: the tenant's subscription does not "
            "let it keep types of its own"
        },
        status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`credential_type_in_use`: something uses it, and the change "
            "would alter who issues it or how a format names it, drop a claim, make one "
            "always present or add one that is"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "as when adding it"},
    },
)
async def update_type(
    tenant_id: TenantId, type_id: uuid.UUID, body: CredentialTypePatch, db: DbSession
) -> CredentialTypeOut:
    item, owner = await _owned(db, tenant_id, type_id)
    current = trust_anchor.type_data(item)
    merged = {**current, **body.model_dump(include=body.model_fields_set)}
    if merged.get("issuance") == "almena" and "vct" not in body.model_fields_set:
        # Its own vct is derived from the key.
        merged["vct"] = None
    try:
        whole = CredentialTypeIn(**merged)
    except ValidationError as error:
        # A field sent as `null` that a type cannot be without.
        raise RequestValidationError(error.errors()) from None
    values = _checked(whole, owner)
    changed = any(values[name] != current[name] for name in NAMING)
    if (changed or not _kept_claims(item.claims, values["claims"])) and await _in_use(
        db, item, owner
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="credential_type_in_use")
    for name, value in values.items():
        setattr(item, name, value)
    await db.commit()
    await db.refresh(item)
    return _out(item, owner)


@router.delete(
    "/{type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of the tenant's credential types, unless something uses it",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "`subscription_required`: the tenant's subscription does not "
            "let it keep types of its own"
        },
        status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`credential_type_in_use`: an issuer grants it, a form asks "
            "for it or an application was made for it"
        },
    },
)
async def delete_type(tenant_id: TenantId, type_id: uuid.UUID, db: DbSession) -> Response:
    # Deleting needs no subscription: what is not used may always go.
    item, owner = await _owned(db, tenant_id, type_id, writes=False)
    if await _in_use(db, item, owner):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="credential_type_in_use")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
