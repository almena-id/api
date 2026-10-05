"""The categories Almena's catalogue files its fields and credential types under.

They are the trust anchor's (`registry_api.trust_anchor`), like the rest of the
catalogue: every tenant sees them (`/catalog/fields`, `/catalog/credentials`)
and files its own credential types under them, but only the anchor's members
add, rename and delete them (`anchor_only` for any other tenant). A category
is for fields (`kind: field`) or for credential types (`kind: credential`); its
key never changes. One that a field or any tenant's credential type is filed
under is not deleted.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import field_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import CatalogCategory, CatalogField, CatalogType, Tenant

router = APIRouter(prefix="/tenants/{tenant_id}/categories", tags=["fields"])

KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
Kind = Literal["field", "credential"]


class CategoryIn(BaseModel):
    kind: Kind
    key: str = Field(max_length=32)
    # In every language of the portal: every tenant reads them.
    labels: dict[str, str]


class CategoryPatch(BaseModel):
    """What changes; the kind and the key never do."""

    labels: dict[str, str]


class CategoryOut(BaseModel):
    id: uuid.UUID
    kind: Kind
    key: str
    labels: dict[str, str]
    # How many fields, or credential types of any tenant, are filed under it.
    uses: int
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


async def _anchor(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    tenant = await db.get_one(Tenant, tenant_id)
    if not tenant.root:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="anchor_only")


def _labels(given: dict[str, str]) -> dict[str, str]:
    kept = {lang: text.strip() for lang, text in given.items()}
    if set(kept) != set(catalog.LANGUAGES) or not all(kept.values()):
        raise _refuse("labels_required")
    if any(len(text) > 100 for text in kept.values()):
        raise _refuse("labels_required")
    return kept


async def _uses(db: AsyncSession, item: CatalogCategory) -> int:
    """Fields of the anchor's, or credential types of any tenant, filed under it."""
    if item.kind == "field":
        query = select(func.count()).where(
            CatalogField.tenant_id == item.tenant_id, CatalogField.category == item.key
        )
    else:
        query = select(func.count()).where(CatalogType.category == item.key)
    return await db.scalar(query) or 0


async def _out(db: AsyncSession, item: CatalogCategory) -> CategoryOut:
    return CategoryOut(
        id=item.id,
        kind=cast(Kind, item.kind),
        key=item.key,
        labels=item.labels,
        uses=await _uses(db, item),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _owned(db: AsyncSession, tenant_id: uuid.UUID, category_id: uuid.UUID) -> CatalogCategory:
    await _anchor(db, tenant_id)
    item = await db.get(CatalogCategory, category_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="category_not_found")
    return item


@router.get(
    "",
    summary="The trust anchor's categories, in the catalogue's order",
    responses={status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"}},
)
async def list_categories(tenant_id: TenantId, db: DbSession) -> list[CategoryOut]:
    await _anchor(db, tenant_id)
    items = await db.scalars(
        select(CatalogCategory)
        .where(CatalogCategory.tenant_id == tenant_id)
        .order_by(CatalogCategory.kind.desc(), CatalogCategory.created_at, CatalogCategory.key)
    )
    return [await _out(db, item) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a category to Almena's catalogue (the trust anchor only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_409_CONFLICT: {"description": "`key_exists`: one of that kind has it"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`key_invalid`, `labels_required` (every language, 100 at most)"
        },
    },
)
async def create_category(tenant_id: TenantId, body: CategoryIn, db: DbSession) -> CategoryOut:
    await _anchor(db, tenant_id)
    if not KEY.match(body.key):
        raise _refuse("key_invalid")
    labels = _labels(body.labels)
    exists = await db.scalar(
        select(CatalogCategory.id).where(
            CatalogCategory.tenant_id == tenant_id,
            CatalogCategory.kind == body.kind,
            CatalogCategory.key == body.key,
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="key_exists")
    item = CatalogCategory(
        tenant_id=tenant_id,
        kind=body.kind,
        key=body.key,
        labels=labels,
        # Set here, to the microsecond: the catalogue is listed in this order.
        created_at=datetime.now(UTC),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.patch(
    "/{category_id}",
    summary="Rename a category (the trust anchor only); never its kind or key",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_404_NOT_FOUND: {"description": "`category_not_found`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "`labels_required`"},
    },
)
async def update_category(
    tenant_id: TenantId, category_id: uuid.UUID, body: CategoryPatch, db: DbSession
) -> CategoryOut:
    item = await _owned(db, tenant_id, category_id)
    item.labels = _labels(body.labels)
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a category nothing is filed under (the trust anchor only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`anchor_only`"},
        status.HTTP_404_NOT_FOUND: {"description": "`category_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`category_in_use`: a field, or a credential type of any "
            "tenant, is filed under it"
        },
    },
)
async def delete_category(tenant_id: TenantId, category_id: uuid.UUID, db: DbSession) -> Response:
    item = await _owned(db, tenant_id, category_id)
    if await _uses(db, item):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="category_in_use")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
