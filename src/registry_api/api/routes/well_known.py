"""The identity domain's own DID and its DID configuration.

``did.jsonl`` is the root tenant's identity's did:webvh log
(``did:webvh:{SCID}:almena.id``) and ``did.json`` its current document under
``did:web:almena.id``, built like any other identity's; ``404`` until the root
exists (``registry-api init-root``) and one of its admins has signed it.
``did-configuration.json`` holds a Domain Linkage Credential signed with the
DID's key, which the API does not hold, so it is produced elsewhere and served
as it is from ``REGISTRY_WELL_KNOWN_DIR``. ``security.txt`` (RFC 9116) says
where to report a vulnerability, for the identity domain and the API's own.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from registry_api import dids, webvh
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.did_documents import log_response, web_response
from registry_api.config import get_settings
from registry_api.root import root_tenant

router = APIRouter(prefix="/.well-known", tags=["did"])

# Where vulnerabilities are reported: privately, through the repository's GitHub.
REPOSITORY = "https://github.com/almena-id/api"
# security.txt must expire, in less than a year; written on each request, it
# stays this far ahead while the API runs.
SECURITY_TXT_TTL = timedelta(days=180)


async def _root_log(db: DbSession) -> list[webvh.Entry]:
    root = await root_tenant(db)
    entries = (
        [] if root is None or root.identity_id is None else await dids.log(db, root.identity_id)
    )
    if not entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return entries


@router.get(
    "/did.jsonl",
    summary="The identity domain's did:webvh log: the root tenant's",
    response_class=Response,
)
async def did_log(db: DbSession) -> Response:
    return log_response(await _root_log(db))


@router.get(
    "/did.json",
    summary="The identity domain's DID document (did:web): the root tenant's",
    response_model=dict[str, Any],
)
async def did_document(db: DbSession) -> JSONResponse:
    return web_response(await _root_log(db))


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


@router.get(
    "/security.txt",
    summary="Where to report a vulnerability (RFC 9116)",
    tags=["security"],
    response_class=PlainTextResponse,
)
async def security_txt() -> PlainTextResponse:
    expires = (datetime.now(UTC) + SECURITY_TXT_TTL).replace(microsecond=0)
    return PlainTextResponse(
        f"Contact: {REPOSITORY}/security/advisories/new\n"
        f"Expires: {expires.isoformat().replace('+00:00', 'Z')}\n"
        f"Policy: {REPOSITORY}/security/policy\n"
        "Preferred-Languages: en, es\n"
    )
