"""The identity domain's own DID document and its DID configuration.

``did.json`` is the root tenant's identity (``did:web:almena.id``), built like
any other identity's; ``404`` until the root exists (``registry-api init-root``).
``did-configuration.json`` holds a Domain Linkage Credential signed with the
DID's key, which the API does not hold, so it is produced elsewhere and served
as it is from ``REGISTRY_WELL_KNOWN_DIR``.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse

from registry_api import dids
from registry_api.api.routes.auth import DbSession
from registry_api.config import get_settings
from registry_api.models import Identity
from registry_api.root import root_tenant

router = APIRouter(prefix="/.well-known", tags=["did"])


@router.get(
    "/did.json",
    summary="The identity domain's DID document (did:web): the root tenant's",
    response_model=dict[str, Any],
)
async def did_document(db: DbSession) -> JSONResponse:
    root = await root_tenant(db)
    identity = (
        None
        if root is None or root.identity_id is None
        else await db.get(Identity, root.identity_id)
    )
    if identity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return JSONResponse(
        await dids.document_for(db, get_settings().did_url, identity),
        media_type="application/did+json",
    )


@router.get(
    "/did-configuration.json",
    summary="The origin's DID configuration (DIF Well Known DID Configuration)",
)
async def did_configuration() -> FileResponse:
    directory = get_settings().well_known_dir
    path = Path(directory) / "did-configuration.json" if directory else None
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return FileResponse(path, media_type="application/json")
