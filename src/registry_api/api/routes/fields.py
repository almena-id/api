"""Almena's catalogues, published: the trust anchor's fields
(`registry_api.field_catalog`) and credential types
(`registry_api.credential_catalog`), as it keeps them (`registry_api.trust_anchor`).

On the identity domain, which proxies them here: ``/schemas/fields/{version}``
and ``/schemas/credentials/{version}`` — each catalogue, and each entry's JSON
Schema at ``…/{id}.json``, where the schemas' `$id` points — and the SD-JWT VC
Type Metadata of Almena's credential types at ``/.well-known/vct/{path}``,
where their `vct` resolves. In the API as ``/catalog/fields`` and
``/catalog/credentials``. Public.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from registry_api import credential_catalog as credentials
from registry_api import field_catalog as catalog
from registry_api import trust_anchor
from registry_api.api.routes.auth import DbSession
from registry_api.models import Tenant

router = APIRouter(tags=["fields"])
api = APIRouter(prefix="/catalog", tags=["fields"])

SCHEMA_JSON = "application/schema+json"


def _version(version: str) -> None:
    if version != catalog.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")


@api.get("/fields", summary="The field catalogue: its fields, groups and value domains")
async def catalogue(db: DbSession) -> dict[str, Any]:
    return (await trust_anchor.load(db)).fields_out()


@router.get(
    "/schemas/fields/{version}",
    summary="The field catalogue, on the identity domain",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`version_not_found`"}},
)
async def published_catalogue(version: str, db: DbSession) -> dict[str, Any]:
    _version(version)
    return (await trust_anchor.load(db)).fields_out()


@router.get(
    "/schemas/fields/{version}/{name}",
    summary="One field's JSON Schema (`{id}.json`)",
    response_class=JSONResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "`field_not_found`"}},
)
async def field_schema(version: str, name: str, db: DbSession) -> JSONResponse:
    _version(version)
    found = (await trust_anchor.load(db)).fields
    item = found.get(name.removesuffix(".json")) if name.endswith(".json") else None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="field_not_found")
    return JSONResponse(catalog.field_schema(item), media_type=SCHEMA_JSON)


@api.get("/credentials", summary="The credential type catalogue: its types and their claims")
async def credential_catalogue(db: DbSession) -> dict[str, Any]:
    return (await trust_anchor.load(db)).types_out()


@router.get(
    "/schemas/credentials/{version}",
    summary="The credential type catalogue, on the identity domain",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`version_not_found`"}},
)
async def published_credentials(version: str, db: DbSession) -> dict[str, Any]:
    if version != credentials.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")
    return (await trust_anchor.load(db)).types_out()


async def _seen_from(db: DbSession, slug: str | None) -> trust_anchor.Catalogue:
    """The catalogue as the tenant `slug` sees it (its own types with
    Almena's); Almena's alone with none. 404 for an unknown tenant."""
    if slug is None:
        return await trust_anchor.load(db)
    tenant_id = await db.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return await trust_anchor.load(db, tenant_id)


@router.get(
    "/schemas/credentials/{version}/{name:path}",
    summary="The JSON Schema of a credential type's claims (`{id}.json`; a tenant's "
    "own, `{tenant_slug}/{key}.json`)",
    response_class=JSONResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"}},
)
async def credential_schema(version: str, name: str, db: DbSession) -> JSONResponse:
    if version != credentials.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")
    slug, _, key = name.removesuffix(".json").rpartition("/")
    if not name.endswith(".json") or "/" in slug:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    found = await _seen_from(db, slug or None)
    item = found.types.get(trust_anchor.PREFIX + key if slug else key)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return JSONResponse(credentials.claims_schema(item, found.fields), media_type=SCHEMA_JSON)


@router.get(
    "/.well-known/vct/{path:path}",
    summary="SD-JWT VC Type Metadata of a credential type of the identity domain: Almena's "
    "(`credentials/{key}/v1`) or a tenant's own (`credentials/{tenant_slug}/{key}/v1`)",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"}},
)
async def type_metadata(path: str, db: DbSession) -> dict[str, Any]:
    parts = path.split("/")
    found = await _seen_from(db, parts[1] if len(parts) == 4 else None)
    item = credentials.by_vct_path(found.types, path)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return credentials.type_metadata(item, found.fields)
