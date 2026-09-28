"""The DID documents the tenants' identities publish, as did:web resolves them.

Public, outside ``/api/v1``: a did:web path is part of the DID and cannot move.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from registry_api import dids
from registry_api.api.routes.auth import DbSession
from registry_api.config import get_settings
from registry_api.models import Identity

router = APIRouter(tags=["did"])


@router.get(
    f"/{dids.PATH}/{{slug}}/did.json",
    summary="An identity's DID document (did:web)",
    response_model=dict[str, Any],
)
async def did_document(slug: str, db: DbSession) -> JSONResponse:
    identity = await db.scalar(select(Identity).where(Identity.slug == slug))
    # The root's is the identity domain's own DID, at /.well-known/did.json.
    # Drafts are seen only inside their tenant.
    if (
        identity is None
        or await dids.is_root_identity(db, identity.id)
        or await dids.is_draft(db, identity.id)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return JSONResponse(
        await dids.document_for(db, get_settings().did_url, identity),
        media_type="application/did+json",
    )
