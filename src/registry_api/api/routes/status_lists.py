"""Issuers' status lists (see `registry_api.status_lists`): whether the
credentials they issued still hold.

- ``GET /status-lists/{slug}``, public, outside /api/v1: the list as last
  signed, the Status List Token verifiers fetch (`application/statuslist+jwt`);
  not found until it is signed.
- ``GET /tenants/{id}/issuers/{id}/status-lists``: the issuer's lists, how
  many statuses each holds, and whether it must be signed (again).
- ``POST …/status-lists/sign``: a wallet request for the issuer's signer to
  sign a list as it is — the current one, made if the issuer has none, before
  its first credential is issued, or one signed with a key the issuer's DID no
  longer lists.
- ``POST /tenants/{id}/applications/{id}/credential-status``: suspend an
  issued credential, reinstate it, or revoke it for good; a wallet request for
  the issuer's signer to sign the list that says so. The change holds once
  signed (`routes.wallet`).
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import status_lists
from registry_api.api.routes.applications import _received
from registry_api.api.routes.auth import CurrentSession, DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.issuance import signing_key
from registry_api.api.routes.issuer_credentials import _issuer
from registry_api.api.routes.wallet import (
    LocaleIn,
    RequestOut,
    SignObject,
    StatusChange,
    new_sign_request,
)
from registry_api.models import Application, Issuer, StatusList, Tenant

public = APIRouter(tags=["status lists"])
router = APIRouter(
    prefix="/tenants/{tenant_id}/issuers/{issuer_id}/status-lists", tags=["status lists"]
)
credentials = APIRouter(prefix="/tenants/{tenant_id}/applications", tags=["status lists"])

STATUS_LIST_JWT = "application/statuslist+jwt"


@public.get(
    "/status-lists/{slug}",
    summary="An issuer's status list, as its signer signed it (a Status List Token)",
    response_class=Response,
    responses={status.HTTP_404_NOT_FOUND: {"description": "`not_found`: none, or not signed yet"}},
)
async def get_public(slug: str, db: DbSession) -> Response:
    item = await db.scalar(select(StatusList).where(StatusList.slug == slug))
    if item is None or item.token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    # Verifiers may keep it a minute: a change is seen that soon.
    return Response(
        item.token, media_type=STATUS_LIST_JWT, headers={"Cache-Control": "public, max-age=60"}
    )


async def _list_out(db: AsyncSession, item: StatusList, issuer: Issuer) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "slug": item.slug,
        "uri": status_lists.uri(item),
        "size": item.size,
        "bits": status_lists.BITS,
        "used": len(await status_lists.used(db, item)),
        **status_lists.counts(item.statuses),
        "revision": item.revision,
        "signed_at": item.signed_at.isoformat() if item.signed_at else None,
        "needs_signing": await status_lists.needs_signing(db, item, issuer),
    }


@router.get("", summary="The issuer's status lists, and whether each must be signed")
async def list_lists(
    tenant_id: TenantId, issuer_id: uuid.UUID, session: CurrentSession, db: DbSession
) -> dict[str, Any]:
    issuer = await _issuer(db, tenant_id, issuer_id)
    return {
        "items": [
            await _list_out(db, item, issuer) for item in await status_lists.lists_of(db, issuer)
        ],
        # The one asking signs, or why not.
        "can_sign": await signing_key(db, issuer, session.user_id) is not None,
        "signer_needed": issuer.signing != "single_user" or issuer.signer_id is None,
    }


async def _ask(
    db: AsyncSession,
    session: CurrentSession,
    asked: LocaleIn,
    issuer: Issuer,
    item: StatusList,
    data: bytes,
    change: StatusChange | None,
    extra: dict[str, Any],
) -> RequestOut:
    key = await signing_key(db, issuer, session.user_id)
    did = issuer.identity.did
    if key is None or did is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_the_issuers_signer")
    tenant = await db.get_one(Tenant, issuer.tenant_id)
    sign = SignObject(
        kind="status_list",
        identity=issuer.name,
        tenant=tenant.name,
        did=did,
        signers=[key],
        verification_method=did,
        document=status_lists.document(item, data, did, key),
        status_change=change,
    )
    extra = {"status_list_id": str(item.id), "revision": item.revision, **extra}
    return await new_sign_request(db, session.user_id, asked, sign, extra)


class SignListIn(LocaleIn):
    # The list to sign; the issuer's current one (made if it has none) if absent.
    status_list_id: uuid.UUID | None = None


@router.post(
    "/sign",
    summary="Ask the issuer's signer's wallet to sign a status list as it is",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_the_issuers_signer`"},
        status.HTTP_404_NOT_FOUND: {"description": "`status_list_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`up_to_date`: signed by a key in force"},
    },
)
async def sign_list(
    tenant_id: TenantId,
    issuer_id: uuid.UUID,
    body: SignListIn,
    session: CurrentSession,
    db: DbSession,
) -> RequestOut:
    issuer = await _issuer(db, tenant_id, issuer_id)
    if await signing_key(db, issuer, session.user_id) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_the_issuers_signer")
    if body.status_list_id is None:
        item = await status_lists.open_list(db, issuer)
    else:
        found = await db.get(StatusList, body.status_list_id)
        if found is None or found.issuer_id != issuer.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="status_list_not_found")
        item = found
    if not await status_lists.needs_signing(db, item, issuer):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="up_to_date")
    return await _ask(db, session, body, issuer, item, item.statuses, None, {})


class CredentialStatusIn(LocaleIn):
    # `revoked` is final; `suspended` until set `valid` again.
    status: Literal["valid", "suspended", "revoked"]


@credentials.post(
    "/{application_id}/credential-status",
    summary="Suspend, reinstate or revoke an issued credential "
    "(the issuer's signer signs the status list that says so)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_the_issuers_signer`"},
        status.HTTP_404_NOT_FOUND: {"description": "`application_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`not_issued`; `no_status_list`: issued before status lists; "
            "`credential_revoked`: revoking is final; `status_unchanged`"
        },
    },
)
async def change_status(
    tenant_id: TenantId,
    application_id: uuid.UUID,
    body: CredentialStatusIn,
    session: CurrentSession,
    db: DbSession,
) -> RequestOut:
    item: Application = await _received(db, tenant_id, application_id)
    if item.status != "issued":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not_issued")
    listed = await db.get(StatusList, item.status_list_id) if item.status_list_id else None
    if listed is None or item.status_index is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no_status_list")
    allowed = status_lists.next_statuses(item.credential_status)
    if not allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="credential_revoked")
    if body.status not in allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="status_unchanged")
    issuer = await db.get_one(Issuer, item.issuer_id)
    data = status_lists.with_value(
        listed.statuses, item.status_index, status_lists.STATUSES[body.status]
    )
    change = StatusChange(index=item.status_index, status=body.status, holder=item.holder_did)
    extra = {"application_id": str(item.id), "credential_status": body.status}
    return await _ask(db, session, body, issuer, listed, data, change, extra)
