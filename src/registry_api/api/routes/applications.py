"""Applications: a holder asks an issuer for a credential.

An issuer *offers* a credential type when it grants it and has a form for it
(`issuer_credentials`); the public catalogue lists its offers
(``GET /catalog/issuers/{slug}/offers/{type}``). A holder applies through the
portal — they have no account here, and need none:

1. **Start** (``POST /applications``): the portal gets the application and a
   secret, which it keeps; every holder-side call carries it
   (``X-Application-Secret``).
2. **Pair** (QR 1): the portal asks for a wallet request (``…/wallet``,
   `pair`) and shows it as ``almena://auth?request_uri=…``. The wallet reads it
   (``GET /applications/{id}/request``) and answers by ``direct_post`` with an
   ``id_token`` by the `did:key` it keeps for this issuer: the application is
   then that holder's.
3. **Fill in**: present credentials the form asks for (`present`: an OpenID4VP
   request with the form's DCQL query, answered with a ``vp_token``, verified
   by `registry_api.presentations`; the fields they fill come from them), type
   the rest (``PUT …/answers``, checked by `registry_api.answers`) and upload
   files (``POST …/files``).
4. **Submit** (QR 2, `submit`): the wallet reads what it is asked to sign — the
   application's content, readable, and its digest (SHA-256 of its JCS) —
   checks the digest, and answers with a JWS by the same `did:key` over it.
   Submitted, it is the issuer's to decide.
5. **Receive**: once accepted and issued (`routes.issuance`), the portal asks
   for a `receive` request (QR 3); the wallet answers with an ``id_token`` by
   the `did:key` it paired with — the one the credential is bound to — and the
   answer to that post is the credential itself (an SD-JWT VC with all its
   disclosures). It can be taken again, by that key only.

The issuer's tenant reads submitted applications, their files and the
holder's signature, and accepts or rejects them (``/tenants/{id}/applications``).
Telling the holder over DIDComm comes next.

Unsubmitted applications expire after a day.
"""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, quote

import jwt
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Form as FormField,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import answers as checks
from registry_api import credential_catalog as credentials
from registry_api import field_catalog as catalog
from registry_api import form_credentials, presentations, wallet, webvh
from registry_api.api.routes.auth import DbSession, as_utc
from registry_api.api.routes.directory import TenantId
from registry_api.config import get_settings
from registry_api.models import Application, ApplicationFile, Form, Issuer
from registry_api.security import digest, new_token

router = APIRouter(prefix="/applications", tags=["applications"])
offers = APIRouter(prefix="/catalog/issuers", tags=["applications"])
inbox = APIRouter(prefix="/tenants/{tenant_id}/applications", tags=["applications"])

TTL = timedelta(days=1)
WALLET_TTL = timedelta(minutes=5)
MAX_FILE = 10 * 1024 * 1024
Purpose = Literal["pair", "present", "submit", "receive"]
StatusFetch = Annotated[presentations.StatusFetch, Depends(presentations.get_status_fetch)]


def _refuse(code: str, status_code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code, detail=code)


def _client_id() -> str:
    """Who the wallet answers: the portal, as for sign-in."""
    return get_settings().portal_url.rstrip("/")


# The offer -------------------------------------------------------------------


async def _offering(db: AsyncSession, slug: str, type_id: str) -> tuple[Issuer, Form]:
    issuer = await db.scalar(select(Issuer).where(Issuer.slug == slug))
    form_id = (issuer.request_forms or {}).get(type_id) if issuer else None
    if (
        issuer is None
        or issuer.published_at is None
        or issuer.identity.did is None
        or type_id not in (issuer.credential_types or [])
        or form_id is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="offer_not_found")
    form = await db.get(Form, uuid.UUID(form_id))
    if form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="offer_not_found")
    return issuer, form


async def offer_view(db: AsyncSession, issuer: Issuer, form: Form, type_id: str) -> dict[str, Any]:
    """What a holder sees of an offer: the issuer, the credential type and the
    form — its fields with their definitions, the credentials it asks for."""
    filled = form_credentials.fills(form.credentials, form.fields)
    return {
        "issuer": {
            "slug": issuer.slug,
            "name": issuer.name,
            "description": issuer.description,
            "did": issuer.identity.did,
        },
        "credential_type": credentials.type_out(credentials.BY_ID[type_id]),
        "form": {
            "slug": form.slug,
            "name": form.name,
            "description": form.description,
            "fields": [
                {**stored, "key": key, "field": catalog.field_out(item)}
                for key, stored, item in await checks.fields_of(db, form)
            ],
            "credentials": [{**entry, "fills": filled[entry["key"]]} for entry in form.credentials],
        },
    }


@offers.get(
    "/{issuer_slug}/offers/{type_id}",
    summary="Public: an issuer's offer of a credential type, with its form",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`offer_not_found`"}},
)
async def get_offer(issuer_slug: str, type_id: str, db: DbSession) -> dict[str, Any]:
    issuer, form = await _offering(db, issuer_slug, type_id)
    return await offer_view(db, issuer, form, type_id)


# The holder's side -----------------------------------------------------------


class StartIn(BaseModel):
    issuer: str = Field(max_length=64)
    type: str = Field(max_length=64)


class Started(BaseModel):
    id: uuid.UUID
    slug: str
    # Whoever started it keeps it: it is the only way back in.
    secret: str


async def _mine(
    application_id: uuid.UUID,
    db: DbSession,
    secret: Annotated[str, Header(alias="X-Application-Secret", max_length=256)],
) -> Application:
    item = await db.get(Application, application_id)
    if item is None or not secrets.compare_digest(item.secret_hash, digest(secret)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="application_not_found")
    if item.status in ("open", "paired") and as_utc(item.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, detail="application_expired")
    return item


Mine = Annotated[Application, Depends(_mine)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Public: start an application for an offer",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`offer_not_found`"}},
)
async def start(body: StartIn, db: DbSession) -> Started:
    issuer, form = await _offering(db, body.issuer, body.type)
    now = datetime.now(UTC)
    # Unsubmitted applications go as new ones come.
    await db.execute(
        delete(Application).where(
            Application.status.in_(("open", "paired")), Application.expires_at <= now
        )
    )
    secret = new_token()
    item = Application(
        tenant_id=issuer.tenant_id,
        issuer_id=issuer.id,
        form_id=form.id,
        credential_type=body.type,
        status="open",
        secret_hash=digest(secret),
        wallet_answered=False,
        answers={},
        presented=[],
        expires_at=now + TTL,
    )
    db.add(item)
    await db.commit()
    return Started(id=item.id, slug=item.slug, secret=secret)


async def _files(db: AsyncSession, item: Application) -> dict[str, dict[str, Any]]:
    rows = await db.scalars(
        select(ApplicationFile).where(ApplicationFile.application_id == item.id)
    )
    return {
        row.key: {
            "filename": row.filename,
            "media_type": row.media_type,
            "size": row.size,
            "digest": row.digest,
        }
        for row in rows
    }


async def _parts(db: AsyncSession, item: Application) -> tuple[Issuer, Form]:
    issuer = await db.get(Issuer, item.issuer_id)
    form = await db.get(Form, item.form_id)
    assert issuer is not None and form is not None
    return issuer, form


def _filled(item: Application) -> dict[str, Any]:
    """The fields verified credentials filled, with their values."""
    values: dict[str, Any] = {}
    for result in item.presented:
        if result.get("verified"):
            values.update(result.get("fills", {}))
    return values


def _deep_link(item: Application) -> str:
    uri = f"{get_settings().public_url.rstrip('/')}/api/v1/applications/{item.id}/request"
    return f"almena://auth?request_uri={quote(uri, safe='')}"


async def _view(db: AsyncSession, item: Application) -> dict[str, Any]:
    issuer, form = await _parts(db, item)
    live = item.wallet_expires_at is not None and as_utc(item.wallet_expires_at) > datetime.now(UTC)
    return {
        "id": str(item.id),
        "slug": item.slug,
        "status": item.status,
        "holder_did": item.holder_did,
        "offer": await offer_view(db, issuer, form, item.credential_type),
        "answers": item.answers,
        "filled": _filled(item),
        "files": await _files(db, item),
        "presented": item.presented,
        "wallet": {
            "purpose": item.wallet_purpose,
            "answered": item.wallet_answered,
            "live": live,
            "deep_link": _deep_link(item) if live and not item.wallet_answered else None,
            "expires_at": item.wallet_expires_at.isoformat() if item.wallet_expires_at else None,
        },
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "decision_note": item.decision_note,
        "issued_at": item.issued_at.isoformat() if item.issued_at else None,
        "valid_until": item.credential_valid_until.isoformat()
        if item.credential_valid_until
        else None,
        "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
    }


@router.get(
    "/{application_id}",
    summary="The application, for whoever holds its secret",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`application_not_found`"},
        status.HTTP_410_GONE: {"description": "`application_expired`"},
    },
)
async def get_application(item: Mine, db: DbSession) -> dict[str, Any]:
    return await _view(db, item)


def _paired(item: Application) -> None:
    if item.status != "paired":
        raise _refuse("application_not_paired" if item.status == "open" else "application_closed")


class AnswersIn(BaseModel):
    answers: dict[str, Any] = Field(max_length=200)


@router.put(
    "/{application_id}/answers",
    summary="Save the typed answers, checked against the form",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`application_not_paired`, `application_closed`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`{code: answers_invalid, errors: {field: problem}}`"
        },
    },
)
async def save_answers(item: Mine, body: AnswersIn, db: DbSession) -> dict[str, Any]:
    _paired(item)
    _, form = await _parts(db, item)
    kept, errors = checks.check(
        await checks.fields_of(db, form),
        body.answers,
        await _files(db, item),
        set(_filled(item)),
    )
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "answers_invalid", "errors": errors},
        )
    item.answers = kept
    await db.commit()
    return await _view(db, item)


@router.post(
    "/{application_id}/files",
    summary="Upload a file for one of the form's file fields (replaces the last)",
    responses={
        status.HTTP_409_CONFLICT: {"description": "`application_not_paired`, `application_closed`"},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "`file_too_large` (10 MB)"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`file_field_unknown`, `file_type_invalid`, `file_empty`"
        },
    },
)
async def upload_file(
    item: Mine,
    db: DbSession,
    key: Annotated[str, FormField(max_length=64)],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    _paired(item)
    _, form = await _parts(db, item)
    found = {k: (stored, field) for k, stored, field in await checks.fields_of(db, form)}
    if key not in found or found[key][1].type != "file":
        raise _refuse("file_field_unknown", status.HTTP_422_UNPROCESSABLE_CONTENT)
    stored, field = found[key]
    allowed = catalog.media_types(
        stored.get("narrow", {}).get("values") or catalog.domain_values(field)
    )
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    if media_type not in allowed:
        raise _refuse("file_type_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT)
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise _refuse("file_too_large", status.HTTP_413_CONTENT_TOO_LARGE)
    if not data:
        raise _refuse("file_empty", status.HTTP_422_UNPROCESSABLE_CONTENT)
    await db.execute(
        delete(ApplicationFile).where(
            ApplicationFile.application_id == item.id, ApplicationFile.key == key
        )
    )
    meta = {
        "filename": (file.filename or key)[-255:],
        "media_type": media_type,
        "size": len(data),
        "digest": "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode(),
    }
    db.add(ApplicationFile(application_id=item.id, key=key, data=data, **meta))
    await db.commit()
    return meta


@router.delete(
    "/{application_id}/files/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an uploaded file",
)
async def remove_file(item: Mine, key: str, db: DbSession) -> Response:
    _paired(item)
    await db.execute(
        delete(ApplicationFile).where(
            ApplicationFile.application_id == item.id, ApplicationFile.key == key
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# What the holder signs -------------------------------------------------------


def _label(labels: dict[str, str]) -> dict[str, str]:
    return {
        lang: labels.get(lang) or labels.get("en") or next(iter(labels.values()), "")
        for lang in catalog.LANGUAGES
    }


def _text(item: catalog.Field, value: Any, lang: str) -> str:
    """An answer as a person reads it, in `lang`."""
    if item.type in ("code", "codes"):
        codes = item.codes or (catalog.domains()[item.domain].codes if item.domain else ())
        names = {code.value: _label(code.labels)[lang] for code in codes}
        chosen = value if isinstance(value, list) else [value]
        return ", ".join(names.get(one, str(one)) for one in chosen)
    if item.type == "group" and isinstance(value, dict):
        return ", ".join(
            _text(part.field, value[part.key], lang) for part in item.parts if part.key in value
        )
    if item.type == "file" and isinstance(value, dict):
        return str(value.get("filename", ""))
    return str(value)


async def content(db: AsyncSession, item: Application) -> dict[str, Any]:
    """What the holder signs: every answer (typed or from a credential),
    readable in each language, the files by digest and the credentials
    presented — so the wallet can show it and the issuer can check it."""
    issuer, form = await _parts(db, item)
    files = await _files(db, item)
    filled = _filled(item)
    shown = []
    for key, _, field in await checks.fields_of(db, form):
        value = files.get(key) if field.type == "file" else filled.get(key, item.answers.get(key))
        if value is None:
            continue
        shown.append(
            {
                "key": key,
                "label": _label(field.labels),
                "value": value,
                "text": {lang: _text(field, value, lang) for lang in catalog.LANGUAGES},
                "verified": key in filled,
            }
        )
    return {
        "application": item.slug,
        "issuer": issuer.identity.did,
        "credential_type": item.credential_type,
        "form": form.slug,
        "answers": shown,
        "credentials": [
            {"key": r["key"], "type": r.get("type"), "issuer": r["issuer"], "claims": r["claims"]}
            for r in item.presented
            if r.get("verified")
        ],
    }


def content_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(webvh.jcs(value)).hexdigest()


# Wallet requests -------------------------------------------------------------


class WalletIn(BaseModel):
    purpose: Purpose


@router.post(
    "/{application_id}/wallet",
    summary="Ask the holder's wallet: pair (QR 1), present credentials, submit (QR 2) "
    "or receive the credential (QR 3)",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "`application_closed`, `application_not_paired`, "
            "`nothing_to_present`, `answers_incomplete`, `not_issued`"
        },
    },
)
async def ask_wallet(item: Mine, body: WalletIn, db: DbSession) -> dict[str, Any]:
    if body.purpose == "receive":
        if item.status != "issued":
            raise _refuse("not_issued")
    elif item.status not in ("open", "paired"):
        raise _refuse("application_closed")
    _, form = await _parts(db, item)
    if body.purpose in ("present", "submit"):
        _paired(item)
    if body.purpose == "present" and not form.credentials:
        raise _refuse("nothing_to_present")
    if body.purpose == "submit":
        filled = _filled(item)
        _, errors = checks.check(
            await checks.fields_of(db, form), item.answers, await _files(db, item), set(filled)
        )
        verified = {r["key"] for r in item.presented if r.get("verified")}
        required = {e["key"] for e in form.credentials if e["required"]}
        if errors or required - verified:
            raise _refuse("answers_incomplete")
    item.wallet_purpose = body.purpose
    item.wallet_nonce = secrets.token_urlsafe(24)
    item.wallet_expires_at = datetime.now(UTC) + WALLET_TTL
    item.wallet_answered = False
    await db.commit()
    return (await _view(db, item))["wallet"]  # type: ignore[no-any-return]


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
    issuer, form = await _parts(db, item)
    purpose = item.wallet_purpose
    kind = credentials.BY_ID[item.credential_type]
    request: dict[str, Any] = {
        "response_type": {
            "pair": "id_token",
            "present": "vp_token",
            "submit": "signature",
            "receive": "credential",
        }[str(purpose)],
        "response_mode": "direct_post",
        "client_id": _client_id(),
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
        request["dcql_query"] = form_credentials.dcql(form.credentials)
    if purpose == "submit":
        signed = await content(db, item)
        request["submission"] = {"content": signed, "digest": content_digest(signed)}
    return request


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
        status.HTTP_409_CONFLICT: {"description": "`already_answered`"},
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def answer_request(
    application_id: uuid.UUID, request: Request, db: DbSession, fetch: StatusFetch
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
                did = wallet.verify(token, audience=_client_id(), nonce=str(item.wallet_nonce))
            except wallet.WalletError as error:
                raise _refuse(error.code, bad) from None
            if item.holder_did not in (None, did):
                # Another wallet: what the last one presented is not its own.
                item.presented = []
            item.holder_did = did
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
            _, form = await _parts(db, item)
            checked = await presentations.verify(
                db,
                form.credentials,
                token,
                nonce=str(item.wallet_nonce),
                audience=_client_id(),
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
            claims = _check_signature(token, item.holder_did, str(item.wallet_nonce))
            if claims.get("application") != item.slug:
                raise _refuse("invalid_signature", bad)
            if claims.get("digest") != expected:
                raise _refuse("digest_mismatch", bad)
            item.status = "submitted"
            item.digest = expected
            item.signature = token
            item.submitted_at = datetime.now(UTC)
    item.wallet_answered = True
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _deliver(db: AsyncSession, item: Application, token: object) -> Response:
    """The credential, to the wallet that proves the key it is bound to."""
    bad = status.HTTP_400_BAD_REQUEST
    if not isinstance(token, str) or item.credential is None:
        raise _refuse("invalid_token", bad)
    try:
        did = wallet.verify(token, audience=_client_id(), nonce=str(item.wallet_nonce))
    except wallet.WalletError as error:
        raise _refuse(error.code, bad) from None
    if did != item.holder_did:
        raise _refuse("not_the_holder", bad)
    issuer, _ = await _parts(db, item)
    kind = credentials.BY_ID[item.credential_type]
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


def _check_signature(token: str, holder: str, nonce: str | None) -> dict[str, Any]:
    """The submission's JWS: by the holder's `did:key`, for the portal and, while
    it is being made, this request's nonce."""
    try:
        issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        raise _refuse("invalid_signature", status.HTTP_400_BAD_REQUEST) from None
    if issuer != holder:
        raise _refuse("not_the_holder", status.HTTP_400_BAD_REQUEST)
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            wallet.public_key(holder),
            algorithms=["EdDSA"],
            audience=_client_id(),
            leeway=wallet.LEEWAY,
            options={"verify_exp": nonce is not None},
        )
    except (jwt.PyJWTError, wallet.WalletError):
        raise _refuse("invalid_signature", status.HTTP_400_BAD_REQUEST) from None
    if nonce is not None and claims.get("nonce") != nonce:
        raise _refuse("invalid_signature", status.HTTP_400_BAD_REQUEST)
    return claims


@router.get(
    "/{application_id}/wallet",
    summary="Whether the wallet has answered the request in course (the portal polls)",
)
async def wallet_state(item: Mine, db: DbSession) -> dict[str, Any]:
    return (await _view(db, item))["wallet"]  # type: ignore[no-any-return]


# The issuer's inbox ----------------------------------------------------------


async def _received(
    db: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> Application:
    item = await db.get(Application, application_id)
    if item is None or item.tenant_id != tenant_id or item.status in ("open", "paired"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="application_not_found")
    return item


@inbox.get("", summary="The applications the tenant's issuers received, newest first")
async def list_received(
    tenant_id: TenantId, db: DbSession, issuer_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = select(Application).where(
        Application.tenant_id == tenant_id, Application.status.not_in(("open", "paired"))
    )
    if issuer_id is not None:
        query = query.where(Application.issuer_id == issuer_id)
    items = await db.scalars(query.order_by(Application.submitted_at.desc(), Application.id.desc()))
    out = []
    for item in items:
        issuer, form = await _parts(db, item)
        out.append(
            {
                "id": str(item.id),
                "slug": item.slug,
                "status": item.status,
                "issuer": {"id": str(issuer.id), "name": issuer.name},
                "credential_type": item.credential_type,
                "form": form.name,
                "holder_did": item.holder_did,
                "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
            }
        )
    return out


@inbox.get(
    "/{application_id}",
    summary="One received application: what the holder signed, and whether it holds",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`application_not_found`"}},
)
async def get_received(
    tenant_id: TenantId, application_id: uuid.UUID, db: DbSession
) -> dict[str, Any]:
    item = await _received(db, tenant_id, application_id)
    issuer, form = await _parts(db, item)
    signed = await content(db, item)
    try:
        claims = _check_signature(str(item.signature), str(item.holder_did), None)
        signature_valid = claims.get("digest") == content_digest(signed) == item.digest
    except HTTPException:
        signature_valid = False
    return {
        "id": str(item.id),
        "slug": item.slug,
        "status": item.status,
        "issuer": {"id": str(issuer.id), "name": issuer.name},
        "form": {"id": str(form.id), "name": form.name},
        "holder_did": item.holder_did,
        "content": signed,
        "files": await _files(db, item),
        "presented": item.presented,
        "digest": item.digest,
        "signature": item.signature,
        "signature_valid": signature_valid,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "decision_note": item.decision_note,
        "issued_at": item.issued_at.isoformat() if item.issued_at else None,
        "valid_until": item.credential_valid_until.isoformat()
        if item.credential_valid_until
        else None,
        "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
    }


@inbox.get(
    "/{application_id}/files/{key}",
    summary="A file the holder uploaded, as uploaded",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`file_not_found`"}},
)
async def get_file(
    tenant_id: TenantId, application_id: uuid.UUID, key: str, db: DbSession
) -> Response:
    item = await _received(db, tenant_id, application_id)
    row = await db.scalar(
        select(ApplicationFile).where(
            ApplicationFile.application_id == item.id, ApplicationFile.key == key
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="file_not_found")
    return Response(
        row.data,
        media_type=row.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


class DecisionIn(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


@inbox.post(
    "/{application_id}/decision",
    summary="Accept or reject a submitted application (any member)",
    responses={status.HTTP_409_CONFLICT: {"description": "`already_decided`"}},
)
async def decide(
    tenant_id: TenantId, application_id: uuid.UUID, body: DecisionIn, db: DbSession
) -> dict[str, Any]:
    item = await _received(db, tenant_id, application_id)
    if item.status != "submitted":
        raise _refuse("already_decided")
    item.status = body.decision
    item.decision_note = (body.note or "").strip() or None
    item.decided_at = datetime.now(UTC)
    await db.commit()
    return await get_received(tenant_id, application_id, db)
