"""Issuing the credential of an accepted application (see `registry_api.issuance`).

1. ``GET …/issuance``: the credential type's claims, each with its value
   proposed from what the holder sent (their answers, and what verified
   credentials filled; the issuer's name as `organization_name`) and whether
   it is always present; until when it would hold (a year); and whether the
   one asking may sign it — the issuer's signer, with a wallet whose key the
   issuer's DID lists.
2. ``PUT …/issuance``: the claims the issuer settles on and until when, each
   checked against its field; kept as the draft to sign. The issuer's signer
   only, as for signing it.
3. ``POST …/issuance/sign``: a wallet request (``purpose: sign``, kind
   `credential`) for the signer's wallet to sign the SD-JWT VC built from the
   draft; its answer issues the credential (`routes.wallet`).
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import answers as checks
from registry_api import credential_catalog, issuance, presentations
from registry_api import field_catalog as catalog
from registry_api.api.routes.applications import _received, content
from registry_api.api.routes.auth import CurrentSession, DbSession, as_utc
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.wallet import (
    LocaleIn,
    RequestOut,
    SignObject,
    my_keys,
    new_sign_request,
)
from registry_api.models import Application, Issuer, Tenant

router = APIRouter(prefix="/tenants/{tenant_id}/applications", tags=["applications"])

VALIDITY = timedelta(days=365)


async def _signing_key(db: AsyncSession, issuer: Issuer, user_id: uuid.UUID) -> str | None:
    """The key `user_id` would sign the issuer's credentials with: theirs, and
    one the issuer's DID lists; `None` if they are not its signer or have none."""
    if issuer.signing != "single_user" or issuer.signer_id != user_id or not issuer.identity.did:
        return None
    listed = {name for name, _ in await presentations.issuer_keys(db, issuer.identity.did)}
    mine = [key for key in await my_keys(db, user_id) if key in listed]
    return mine[0] if mine else None


async def _accepted(
    db: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> tuple[Application, Issuer]:
    item = await _received(db, tenant_id, application_id)
    if item.status != "accepted":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="already_issued" if item.status == "issued" else "not_accepted",
        )
    issuer = await db.get_one(Issuer, item.issuer_id)
    return item, issuer


@router.get(
    "/{application_id}/issuance",
    summary="The credential to issue: its claims, proposed from the application",
    responses={status.HTTP_409_CONFLICT: {"description": "`not_accepted`, `already_issued`"}},
)
async def proposal(
    tenant_id: TenantId, application_id: uuid.UUID, session: CurrentSession, db: DbSession
) -> dict[str, Any]:
    item, issuer = await _accepted(db, tenant_id, application_id)
    kind = credential_catalog.BY_ID[item.credential_type]
    sent = {answer["key"]: answer["value"] for answer in (await content(db, item))["answers"]}
    sent.setdefault("organization_name", issuer.name)
    draft = item.issuance_claims or {}
    valid_until = (
        as_utc(item.credential_valid_until)
        if item.credential_valid_until
        else datetime.now(UTC) + VALIDITY
    )
    key = await _signing_key(db, issuer, session.user_id)
    return {
        "credential_type": credential_catalog.type_out(kind),
        "claims": [
            {
                "field": catalog.field_out(catalog.BY_ID[claim.field]),
                "required": claim.required,
                "value": draft.get(claim.field, sent.get(claim.field)),
            }
            for claim in kind.claims
        ],
        "valid_until": valid_until.date().isoformat(),
        "holder_did": item.holder_did,
        # The one asking signs, or why not.
        "can_sign": key is not None,
        "signer_needed": issuer.signing != "single_user" or issuer.signer_id is None,
    }


class IssuanceIn(BaseModel):
    claims: dict[str, Any] = Field(max_length=50)
    valid_until: date


@router.put(
    "/{application_id}/issuance",
    summary="Settle the credential's claims and validity (the draft its signer signs)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_the_issuers_signer`"},
        status.HTTP_409_CONFLICT: {"description": "`not_accepted`, `already_issued`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`{code: claims_invalid, errors: {claim: problem}}`, "
            "`valid_until_invalid` (not after today)"
        },
    },
)
async def save_draft(
    tenant_id: TenantId,
    application_id: uuid.UUID,
    body: IssuanceIn,
    session: CurrentSession,
    db: DbSession,
) -> dict[str, Any]:
    item, issuer = await _accepted(db, tenant_id, application_id)
    if await _signing_key(db, issuer, session.user_id) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_the_issuers_signer")
    if body.valid_until <= datetime.now(UTC).date():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="valid_until_invalid")
    kind = credential_catalog.BY_ID[item.credential_type]
    kept: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for claim in kind.claims:
        field = catalog.BY_ID[claim.field]
        given = body.claims.get(claim.field)
        if given is None or given == "" or given == [] or given == {}:
            if claim.required:
                errors[claim.field] = "required"
            continue
        value, problem = checks.value_of(field, {}, given, {}, claim.field)
        if problem:
            part, _, code = problem.rpartition(":")
            errors[f"{claim.field}.{part}" if part else claim.field] = code
        else:
            kept[claim.field] = value
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "claims_invalid", "errors": errors},
        )
    item.issuance_claims = kept
    # The end of that day, in UTC.
    item.credential_valid_until = datetime.combine(body.valid_until, time.max, tzinfo=UTC)
    await db.commit()
    return {"claims": kept, "valid_until": body.valid_until.isoformat()}


@router.post(
    "/{application_id}/issuance/sign",
    summary="Ask the issuer's signer's wallet to sign the credential (the draft)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_the_issuers_signer`"},
        status.HTTP_409_CONFLICT: {
            "description": "`not_accepted`, `already_issued`, `no_draft`, `not_paired`"
        },
    },
)
async def ask_signature(
    tenant_id: TenantId,
    application_id: uuid.UUID,
    body: LocaleIn,
    session: CurrentSession,
    db: DbSession,
) -> RequestOut:
    item, issuer = await _accepted(db, tenant_id, application_id)
    if item.issuance_claims is None or item.credential_valid_until is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no_draft")
    if item.holder_did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not_paired")
    key = await _signing_key(db, issuer, session.user_id)
    if key is None or issuer.identity.did is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_the_issuers_signer")
    valid_until = as_utc(item.credential_valid_until)
    signed = issuance.document(
        issuer=issuer.identity.did,
        key=key,
        type_id=item.credential_type,
        holder=item.holder_did,
        claims=item.issuance_claims,
        valid_until=valid_until,
    )
    tenant = await db.get_one(Tenant, tenant_id)
    sign = SignObject(
        kind="credential",
        identity=issuer.name,
        tenant=tenant.name,
        did=issuer.identity.did,
        valid_until=valid_until.isoformat(timespec="seconds").replace("+00:00", "Z"),
        signers=[key],
        verification_method=issuer.identity.did,
        document=signed,
    )
    return await new_sign_request(db, session.user_id, body, sign, {"application_id": str(item.id)})
