"""The wallet's side of an application: the requests the catalog shows as QR
codes (pair, present, submit, receive), the wallet reading and answering them
by ``direct_post``, and collecting an issued credential from the issuer's
notice. See `routes.applications` for the whole flow."""

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

import jwt
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import answers as checks
from registry_api import (
    didcomm,
    form_credentials,
    notices,
    presentations,
    queues,
    trust_anchor,
    wallet,
)
from registry_api.api.routes.applications import (
    Mine,
    check_signature,
    client_id,
    content,
    content_digest,
    deep_link,
    files_of,
    filled_by_credentials,
    paired,
    parts,
    view,
)
from registry_api.api.routes.auth import DbSession, as_utc
from registry_api.config import get_settings
from registry_api.models import (
    Application,
    Form,
)

router = APIRouter(prefix="/applications", tags=["applications"])

WALLET_TTL = timedelta(minutes=5)
Purpose = Literal["pair", "present", "submit", "receive"]
StatusFetch = Annotated[presentations.StatusFetch, Depends(presentations.get_status_fetch)]


def _refuse(code: str, status_code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code, detail=code)


# Wallet requests -------------------------------------------------------------


class WalletIn(BaseModel):
    purpose: Purpose


async def _complete(db: AsyncSession, item: Application, form: Form) -> None:
    """Every answer and required credential in, or `answers_incomplete`."""
    _, errors = checks.check(
        await checks.fields_of(db, form),
        item.answers,
        await files_of(db, item),
        set(filled_by_credentials(item)),
    )
    verified = {r["key"] for r in item.presented if r.get("verified")}
    required = {e["key"] for e in form.credentials if e["required"]}
    if errors or required - verified:
        raise _refuse("answers_incomplete")


@router.post(
    "/{application_id}/wallet",
    summary="Ask the holder's wallet: pair (QR 1), present credentials, submit (QR 2) "
    "or receive the credential (QR 3)",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "`application_closed`, `application_not_paired`, "
            "`nothing_to_present`, `answers_incomplete`, `not_issued`, "
            "`already_paired`, `already_received`"
        },
    },
)
async def ask_wallet(item: Mine, body: WalletIn, db: DbSession) -> dict[str, Any]:
    if body.purpose == "receive":
        if item.status != "issued":
            raise _refuse("not_issued")
        if item.delivered_at is not None:
            raise _refuse("already_received")
    elif item.status not in ("open", "paired"):
        raise _refuse("application_closed")
    if body.purpose == "pair" and item.status != "open":
        raise _refuse("already_paired")
    _, form = await parts(db, item)
    if body.purpose in ("present", "submit"):
        paired(item)
    if body.purpose == "present" and not form.credentials:
        raise _refuse("nothing_to_present")
    if body.purpose == "submit":
        await _complete(db, item, form)
    item.wallet_purpose = body.purpose
    item.wallet_nonce = secrets.token_urlsafe(24)
    item.wallet_expires_at = datetime.now(UTC) + WALLET_TTL
    item.wallet_answered = False
    await db.commit()
    return (await view(db, item))["wallet"]  # type: ignore[no-any-return]


class CollectIn(BaseModel):
    # Signed by the holder's key for this issuer (the one it paired with):
    # audience this very endpoint (the notice's `collect`), nonce the
    # application's id.
    id_token: str = Field(max_length=8192)


@router.post(
    "/{application_id}/collect",
    summary="The holder's wallet asks to receive its issued credential, from a notice",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "`invalid_token`, `invalid_did`"},
        status.HTTP_404_NOT_FOUND: {"description": "`application_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`not_issued`, `already_received`"},
    },
)
async def collect(application_id: uuid.UUID, body: CollectIn, db: DbSession) -> dict[str, Any]:
    """What the `issued` notice points at: the wallet proves it is the holder
    and gets a `receive` request, answered as the catalog's QR 3 is."""
    item = await db.get(Application, application_id)
    if item is None or item.holder_did is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="application_not_found")
    try:
        did = wallet.verify(body.id_token, audience=notices.collect_url(item), nonce=str(item.id))
    except wallet.WalletError as error:
        raise _refuse(error.code, status.HTTP_400_BAD_REQUEST) from None
    if did != item.holder_did:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="application_not_found")
    if item.status != "issued":
        raise _refuse("not_issued")
    if item.delivered_at is not None:
        raise _refuse("already_received")
    item.wallet_purpose = "receive"
    item.wallet_nonce = secrets.token_urlsafe(24)
    item.wallet_expires_at = datetime.now(UTC) + WALLET_TTL
    item.wallet_answered = False
    await db.commit()
    return {
        "request_uri": f"{get_settings().public_url.rstrip('/')}"
        f"/api/v1/applications/{item.id}/request",
        "deep_link": deep_link(item),
        "expires_at": item.wallet_expires_at.isoformat(),
    }


async def _asked(db: AsyncSession, application_id: uuid.UUID) -> Application:
    item = await db.get(Application, application_id)
    if item is None or item.wallet_purpose is None or item.wallet_nonce is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="request_not_found")
    if item.wallet_expires_at is None or as_utc(item.wallet_expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, detail="request_expired")
    if item.wallet_answered:
        raise _refuse("already_answered")
    return item


@router.get(
    "/{application_id}/request",
    summary="The wallet request in course, as the wallet reads it",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`request_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`already_answered`"},
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def read_request(application_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    item = await _asked(db, application_id)
    issuer, form = await parts(db, item)
    purpose = item.wallet_purpose
    types = (await trust_anchor.load(db, issuer.tenant_id)).types
    kind = types[item.credential_type]
    request: dict[str, Any] = {
        "response_type": {
            "pair": "id_token",
            "present": "vp_token",
            "submit": "signature",
            "receive": "credential",
        }[str(purpose)],
        "response_mode": "direct_post",
        "client_id": client_id(),
        "client_name": issuer.name,
        "response_uri": f"{get_settings().public_url.rstrip('/')}"
        f"/api/v1/applications/{item.id}/request/response",
        "nonce": item.wallet_nonce,
        "purpose": purpose,
        "expires_at": item.wallet_expires_at.isoformat() if item.wallet_expires_at else None,
        # The wallet keeps one key per issuer: the one it pairs and signs with.
        "issuer": {"did": issuer.identity.did, "name": issuer.name},
        "credential_type": {"id": kind.id, "labels": kind.labels},
        "holder": item.holder_did,
    }
    if purpose == "present":
        request["dcql_query"] = form_credentials.dcql(types, form.credentials)
    if purpose == "submit":
        signed = await content(db, item)
        request["submission"] = {"content": signed, "digest": content_digest(signed)}
    return request


def _messaging_did(id_token: str) -> str | None:
    """Where the holder receives the issuer's messages: the `did:peer:2` the
    (already verified) pairing token names as `didcomm`, if it does;
    `invalid_didcomm` if it names something else."""
    named = jwt.decode(id_token, options={"verify_signature": False}).get("didcomm")
    if named is None:
        return None
    if not isinstance(named, str):
        raise _refuse("invalid_didcomm", status.HTTP_400_BAD_REQUEST)
    try:
        return didcomm.peer(named).did
    except didcomm.DidCommError as error:
        raise _refuse(error.code, status.HTTP_400_BAD_REQUEST) from None


async def _answer(request: Request) -> dict[str, Any]:
    """The wallet's answer: a form (`direct_post`) or JSON."""
    raw = (await request.body())[: 2 * 1024 * 1024].decode(errors="replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return body if isinstance(body, dict) else {}
    return {key: values[0] for key, values in parse_qs(raw).items()}


@router.post(
    "/{application_id}/request/response",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="The wallet's answer: an `id_token`, a `vp_token` or a `signature`; for "
    "`receive`, an `id_token` answered with the credential",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "`invalid_token`, `invalid_did`, `invalid_vp_token`, "
            "`invalid_signature`, `not_the_holder`, `digest_mismatch`"
        },
        status.HTTP_200_OK: {
            "description": "`receive`: `{format, credential, issuer, credential_type}`"
        },
        status.HTTP_404_NOT_FOUND: {"description": "`request_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`already_answered`, `answers_incomplete`, `already_paired`, "
            "`already_received`"
        },
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def answer_request(
    application_id: uuid.UUID,
    request: Request,
    db: DbSession,
    fetch: StatusFetch,
    broker: notices.BrokerDep,
) -> Response:
    item = await _asked(db, application_id)
    body = await _answer(request)
    bad = status.HTTP_400_BAD_REQUEST
    if item.wallet_purpose == "receive":
        return await _deliver(db, item, body.get("id_token"))
    match item.wallet_purpose:
        case "pair":
            token = body.get("id_token")
            if not isinstance(token, str):
                raise _refuse("invalid_token", bad)
            try:
                did = wallet.verify(token, audience=client_id(), nonce=str(item.wallet_nonce))
            except wallet.WalletError as error:
                raise _refuse(error.code, bad) from None
            if item.status != "open":
                raise _refuse("already_paired")
            item.holder_did = did
            item.holder_messaging_did = _messaging_did(token)
            item.status = "paired"
        case "present":
            token = body.get("vp_token")
            if isinstance(token, str):
                try:
                    token = json.loads(token)
                except ValueError:
                    token = None
            if not isinstance(token, dict) or not all(
                isinstance(v, list) and all(isinstance(p, str) for p in v) for v in token.values()
            ):
                raise _refuse("invalid_vp_token", bad)
            _, form = await parts(db, item)
            checked = await presentations.verify(
                db,
                form.credentials,
                token,
                tenant_id=form.tenant_id,
                nonce=str(item.wallet_nonce),
                audience=client_id(),
                fetch=fetch,
            )
            filled = form_credentials.fills(form.credentials, form.fields)
            types = {entry["key"]: entry["type"] for entry in form.credentials}
            item.presented = [
                {
                    "key": result.key,
                    "type": types[result.key],
                    "presented": result.presented,
                    "verified": result.verified,
                    "format": result.format,
                    "issuer": result.issuer,
                    "claims": result.claims,
                    "fills": {k: result.claims[k] for k in filled[result.key] if k in result.claims}
                    if result.verified
                    else {},
                    "problems": result.problems,
                }
                for result in checked
            ]
        case "submit":
            token = body.get("signature")
            if not isinstance(token, str) or item.holder_did is None:
                raise _refuse("invalid_signature", bad)
            expected = content_digest(await content(db, item))
            claims = check_signature(token, item.holder_did, str(item.wallet_nonce))
            if claims.get("application") != item.slug:
                raise _refuse("invalid_signature", bad)
            if claims.get("digest") != expected:
                raise _refuse("digest_mismatch", bad)
            # What was complete when asked may not be now (a file removed since).
            _, form = await parts(db, item)
            await _complete(db, item, form)
            item.status = "submitted"
            item.digest = expected
            item.signature = token
            item.submitted_at = datetime.now(UTC)
    item.wallet_answered = True
    await db.commit()
    if item.wallet_purpose == "submit":
        # The issuer's back office hears of it, when it has a queue.
        await queues.submitted(db, broker, item, await content(db, item), await files_of(db, item))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _deliver(db: AsyncSession, item: Application, token: object) -> Response:
    """The credential, to the wallet that proves the key it is bound to."""
    bad = status.HTTP_400_BAD_REQUEST
    if not isinstance(token, str) or item.credential is None:
        raise _refuse("invalid_token", bad)
    if item.delivered_at is not None:
        raise _refuse("already_received")
    try:
        did = wallet.verify(token, audience=client_id(), nonce=str(item.wallet_nonce))
    except wallet.WalletError as error:
        raise _refuse(error.code, bad) from None
    if did != item.holder_did:
        raise _refuse("not_the_holder", bad)
    issuer, _ = await parts(db, item)
    kind = (await trust_anchor.load(db, issuer.tenant_id)).types[item.credential_type]
    item.wallet_answered = True
    item.delivered_at = datetime.now(UTC)
    await db.commit()
    return JSONResponse(
        {
            "format": "dc+sd-jwt",
            "credential": item.credential,
            "issuer": {"did": issuer.identity.did, "name": issuer.name},
            "credential_type": {"id": kind.id, "labels": kind.labels},
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/{application_id}/wallet",
    summary="Whether the wallet has answered the request in course (the catalog polls)",
)
async def wallet_state(item: Mine, db: DbSession) -> dict[str, Any]:
    return (await view(db, item))["wallet"]  # type: ignore[no-any-return]
