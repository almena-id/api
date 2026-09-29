"""The DIDs the tenants' identities publish: each one's did:webvh log, its
current document under its did:web name, and — for a published issuer,
verifier or mediator — its `whois.vp`, its tenant's endorsement.

Public, outside ``/api/v1``: the path is part of the DID and cannot move. A
pending identity (nothing signed yet) and a draft's are not found.
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from registry_api import dids, webvh
from registry_api.api.routes.auth import DbSession
from registry_api.models import Identity

router = APIRouter(tags=["did"])

JSONL = "text/jsonl"


async def _public(db: DbSession, slug: str) -> tuple[Identity, list[webvh.Entry]]:
    identity = await db.scalar(select(Identity).where(Identity.slug == slug))
    # The root's is the identity domain's own DID, under /.well-known/.
    if (
        identity is None
        or await dids.is_root_identity(db, identity.id)
        or await dids.is_draft(db, identity.id)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    entries = await dids.log(db, identity.id)
    if not entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return identity, entries


def log_response(entries: list[webvh.Entry]) -> Response:
    lines = "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries)
    return Response(lines, media_type=JSONL)


def web_response(entries: list[webvh.Entry]) -> JSONResponse:
    return JSONResponse(
        webvh.as_did_web(entries[-1]["state"], webvh.scid_of(entries)),
        media_type="application/did+json",
    )


@router.get(
    f"/{dids.PATH}/{{slug}}/did.jsonl",
    summary="An identity's did:webvh log",
    response_class=Response,
)
async def did_log(slug: str, db: DbSession) -> Response:
    _, entries = await _public(db, slug)
    return log_response(entries)


@router.get(
    f"/{dids.PATH}/{{slug}}/did.json",
    summary="An identity's current DID document, under its did:web name",
    response_model=dict[str, Any],
)
async def did_document(slug: str, db: DbSession) -> JSONResponse:
    _, entries = await _public(db, slug)
    return web_response(entries)


@router.get(
    f"/{dids.PATH}/{{slug}}/whois.vp",
    summary="A published issuer's, verifier's or mediator's whois.vp: its tenant's endorsement",
    response_class=Response,
)
async def whois(slug: str, db: DbSession) -> Response:
    identity, _ = await _public(db, slug)
    if identity.presentation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return Response(identity.presentation, media_type="application/vp")
