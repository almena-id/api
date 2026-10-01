"""Almena's catalogues, published: fields (`registry_api.field_catalog`) and
credential types (`registry_api.credential_catalog`).

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

from registry_api import credential_catalog as credentials
from registry_api import field_catalog as catalog

router = APIRouter(tags=["fields"])
api = APIRouter(prefix="/catalog", tags=["fields"])

SCHEMA_JSON = "application/schema+json"


def _version(version: str) -> None:
    if version != catalog.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")


@api.get("/fields", summary="The field catalogue: its fields, groups and value domains")
async def catalogue() -> dict[str, Any]:
    return catalog.catalogue()


@router.get(
    "/schemas/fields/{version}",
    summary="The field catalogue, on the identity domain",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`version_not_found`"}},
)
async def published_catalogue(version: str) -> dict[str, Any]:
    _version(version)
    return catalog.catalogue()


@router.get(
    "/schemas/fields/{version}/{name}",
    summary="One field's JSON Schema (`{id}.json`)",
    response_class=JSONResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "`field_not_found`"}},
)
async def field_schema(version: str, name: str) -> JSONResponse:
    _version(version)
    item = catalog.BY_ID.get(name.removesuffix(".json")) if name.endswith(".json") else None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="field_not_found")
    return JSONResponse(catalog.field_schema(item), media_type=SCHEMA_JSON)


@api.get("/credentials", summary="The credential type catalogue: its types and their claims")
async def credential_catalogue() -> dict[str, Any]:
    return credentials.catalogue()


@router.get(
    "/schemas/credentials/{version}",
    summary="The credential type catalogue, on the identity domain",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`version_not_found`"}},
)
async def published_credentials(version: str) -> dict[str, Any]:
    if version != credentials.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")
    return credentials.catalogue()


@router.get(
    "/schemas/credentials/{version}/{name}",
    summary="The JSON Schema of a credential type's claims (`{id}.json`)",
    response_class=JSONResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"}},
)
async def credential_schema(version: str, name: str) -> JSONResponse:
    if version != credentials.VERSION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version_not_found")
    item = credentials.BY_ID.get(name.removesuffix(".json")) if name.endswith(".json") else None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return JSONResponse(credentials.claims_schema(item), media_type=SCHEMA_JSON)


@router.get(
    "/.well-known/vct/{path:path}",
    summary="SD-JWT VC Type Metadata of one of Almena's credential types",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`credential_type_not_found`"}},
)
async def type_metadata(path: str) -> dict[str, Any]:
    item = credentials.by_vct_path(path)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="credential_type_not_found")
    return credentials.type_metadata(item)
