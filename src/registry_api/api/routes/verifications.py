"""A verifier asks a wallet, by QR, to present what a form asks for.

A member of the tenant opens a verification for one of its published
verifiers and a form with credentials: the API makes an OpenID4VP request —
the form's DCQL query, a nonce, five minutes — shown as
``almena://auth?request_uri=…`` (QR or link), on the same channel as signing
in. The wallet reads it (``GET /verifications/{id}/request``: `purpose`
`verify`, the verifier by name and DID), the person chooses what to present,
and the wallet answers by ``direct_post`` with a `vp_token`, checked as
``POST …/forms/{id}/verify`` checks one (key binding for the registry portal,
the `client_id`, and the request's nonce). The portal polls the verification
until it is answered, and shows the verdict; the verifier's queue hears of it
(`registry_api.queues`). A request is answered once.
"""

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from registry_api import form_credentials, presentations, queues, trust_anchor
from registry_api.api.routes.auth import CurrentSession, DbSession, as_utc
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.forms import VerifyOut, verdict
from registry_api.broker import Broker, get_broker
from registry_api.config import get_settings
from registry_api.models import Form, Verification, Verifier
from registry_api.texts import text_of

router = APIRouter(
    prefix="/tenants/{tenant_id}/verifiers/{verifier_id}/verifications", tags=["verifications"]
)
public = APIRouter(prefix="/verifications", tags=["verifications"])

TTL = timedelta(minutes=5)
StatusFetch = Annotated[presentations.StatusFetch, Depends(presentations.get_status_fetch)]
BrokerDep = Annotated[Broker, Depends(get_broker)]


def _client_id() -> str:
    """Who the wallet answers: the registry portal, where the code is shown."""
    return get_settings().portal_url.rstrip("/")


def _request_uri(item: Verification) -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/v1/verifications/{item.id}/request"


class VerificationIn(BaseModel):
    form_id: uuid.UUID


class VerificationOut(BaseModel):
    id: uuid.UUID
    # `pending` until the wallet answers, then `answered`; `expired` unanswered.
    status: Literal["pending", "answered", "expired"]
    verifier: dict[str, str]
    form: dict[str, Any]
    expires_at: datetime
    # While pending: what the code says.
    deep_link: str | None
    answered_at: datetime | None
    # Once answered: the verdict, as `…/forms/{id}/verify` gives it.
    result: VerifyOut | None


async def _verifier(db: DbSession, tenant_id: uuid.UUID, verifier_id: uuid.UUID) -> Verifier:
    verifier = await db.get(Verifier, verifier_id)
    if verifier is None or verifier.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="verifier_not_found")
    return verifier


def _status(item: Verification) -> Literal["pending", "answered", "expired"]:
    if item.answered_at is not None:
        return "answered"
    return "pending" if as_utc(item.expires_at) > datetime.now(UTC) else "expired"


async def _out(db: DbSession, item: Verification) -> VerificationOut:
    verifier = await db.get_one(Verifier, item.verifier_id)
    form = await db.get_one(Form, item.form_id)
    state = _status(item)
    return VerificationOut(
        id=item.id,
        status=state,
        verifier={
            "id": str(verifier.id),
            "name": verifier.name,
            "did": verifier.identity.did or "",
        },
        form={"id": str(form.id), "name": form.name},
        expires_at=item.expires_at,
        deep_link=f"almena://auth?request_uri={quote(_request_uri(item), safe='')}"
        if state == "pending"
        else None,
        answered_at=item.answered_at,
        result=VerifyOut.model_validate(item.result) if item.result is not None else None,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Ask a wallet, by QR, to present what a form asks for, as one of the verifiers",
    responses={
        404: {"description": "`verifier_not_found`, `form_not_found`"},
        409: {"description": "`verifier_unpublished`, `nothing_to_present`"},
    },
)
async def open_verification(
    tenant_id: TenantId,
    verifier_id: uuid.UUID,
    body: VerificationIn,
    session: CurrentSession,
    db: DbSession,
) -> VerificationOut:
    verifier = await _verifier(db, tenant_id, verifier_id)
    # The wallet is shown who asks: a DID that resolves, endorsed by the tenant.
    if verifier.published_at is None or verifier.identity.did is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="verifier_unpublished")
    form = await db.get(Form, body.form_id)
    if form is None or form.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="form_not_found")
    if not form.credentials:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="nothing_to_present")
    item = Verification(
        tenant_id=tenant_id,
        verifier_id=verifier.id,
        form_id=form.id,
        user_id=session.user_id,
        nonce=secrets.token_urlsafe(24),
        expires_at=datetime.now(UTC) + TTL,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return await _out(db, item)


@router.get(
    "/{verification_id}",
    summary="A verification: pending, answered with its verdict, or expired",
    responses={404: {"description": "`verification_not_found`"}},
)
async def get_verification(
    tenant_id: TenantId, verifier_id: uuid.UUID, verification_id: uuid.UUID, db: DbSession
) -> VerificationOut:
    item = await db.get(Verification, verification_id)
    if item is None or item.tenant_id != tenant_id or item.verifier_id != verifier_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="verification_not_found")
    return await _out(db, item)


async def _live(db: DbSession, verification_id: uuid.UUID) -> Verification:
    item = await db.get(Verification, verification_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="request_not_found")
    if item.answered_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already_answered")
    if as_utc(item.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, detail="request_expired")
    return item


@public.get(
    "/{verification_id}/request",
    summary="The verifier's request, as the wallet reads it",
    responses={
        404: {"description": "`request_not_found`"},
        409: {"description": "`already_answered`"},
        410: {"description": "`request_expired`"},
    },
)
async def read_request(verification_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    item = await _live(db, verification_id)
    verifier = await db.get_one(Verifier, item.verifier_id)
    form = await db.get_one(Form, item.form_id)
    base = get_settings().public_url.rstrip("/")
    return {
        "response_type": "vp_token",
        "response_mode": "direct_post",
        "client_id": _client_id(),
        "client_name": verifier.name,
        "response_uri": f"{base}/api/v1/verifications/{item.id}/request/response",
        "nonce": item.nonce,
        "purpose": "verify",
        "expires_at": item.expires_at.isoformat(),
        "verifier": {"did": verifier.identity.did, "name": verifier.name},
        # What it is for, in the form's languages: the wallet picks its own.
        "form": {"name": form.name, "description": form.description},
        "dcql_query": form_credentials.dcql(
            (await trust_anchor.load(db, form.tenant_id)).types, form.credentials
        ),
        "credentials": [
            {
                "key": entry["key"],
                "type": entry["type"],
                "required": entry["required"],
                "purpose": entry.get("purpose"),
            }
            for entry in form.credentials
        ],
    }


async def _vp_token(request: Request) -> dict[str, list[str]]:
    """The wallet's `vp_token`: a form field (JSON in it) or a JSON body."""
    raw = (await request.body())[: 2 * 1024 * 1024].decode(errors="replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            token: Any = json.loads(raw).get("vp_token")
        except (ValueError, AttributeError):
            token = None
    else:
        values = parse_qs(raw).get("vp_token")
        token = values[0] if values else None
    if isinstance(token, str):
        try:
            token = json.loads(token)
        except ValueError:
            token = None
    if not isinstance(token, dict) or not all(
        isinstance(v, list) and all(isinstance(p, str) for p in v) for v in token.values()
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_vp_token")
    return token


@public.post(
    "/{verification_id}/request/response",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="The wallet's answer: a `vp_token` (OpenID4VP `direct_post`)",
    responses={
        400: {"description": "`invalid_vp_token`"},
        404: {"description": "`request_not_found`"},
        409: {"description": "`already_answered`"},
        410: {"description": "`request_expired`"},
    },
)
async def answer_request(
    verification_id: uuid.UUID,
    request: Request,
    db: DbSession,
    fetch: StatusFetch,
    broker: BrokerDep,
) -> Response:
    item = await _live(db, verification_id)
    token = await _vp_token(request)
    form = await db.get_one(Form, item.form_id)
    checked = await presentations.verify(
        db,
        form.credentials,
        token,
        tenant_id=form.tenant_id,
        nonce=item.nonce,
        audience=_client_id(),
        fetch=fetch,
    )
    result = verdict(form, checked)
    item.answered_at = datetime.now(UTC)
    item.verified = result.verified
    item.result = result.model_dump(mode="json")
    await db.commit()
    verifier = await db.get_one(Verifier, item.verifier_id)
    await queues.presentation_verified(
        broker,
        verifier,
        {
            "verification": {
                "id": str(item.id),
                "form_id": str(form.id),
                "form": text_of(form.name, "en"),
                "answered_at": item.answered_at.isoformat(),
            },
            **result.model_dump(mode="json"),
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
