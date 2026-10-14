"""Applications: a holder asks an issuer for a credential.

An issuer *offers* a credential type when it grants it and has a form for it
(`issuer_credentials`); the public catalogue lists every published issuer's
offers (``GET /catalog/offers``) and opens each
(``GET /catalog/issuers/{slug}/offers/{type}``). A holder applies through the
catalog (``catalog_url``, the wallet's `client_id`) — they have no account
here, and need none:

1. **Start** (``POST /applications``): the catalog gets the application and a
   secret, which it keeps; every holder-side call carries it
   (``X-Application-Secret``).
2. **Pair** (QR 1): the catalog asks for a wallet request (``…/wallet``,
   `pair`) and shows it as ``almena://auth?request_uri=…``. The wallet reads it
   (``GET /applications/{id}/request``) and answers by ``direct_post`` with an
   ``id_token`` by the `did:key` it keeps for this issuer: the application is
   then that holder's, once and for all.
3. **Fill in**: present credentials the form asks for (`present`: an OpenID4VP
   request with the form's DCQL query, answered with a ``vp_token``, verified
   by `registry_api.presentations`; the fields they fill come from them), type
   the rest (``PUT …/answers``, checked by `registry_api.answers`) and upload
   files (``POST …/files``).
4. **Submit** (QR 2, `submit`): the wallet reads what it is asked to sign — the
   application's content, readable, and its digest (SHA-256 of its JCS) —
   checks the digest, and answers with a JWS by the same `did:key` over it.
   Submitted, it is the issuer's to decide.
5. **Receive**: once accepted and issued (`routes.issuance`), the catalog asks
   for a `receive` request (QR 3); the wallet answers with an ``id_token`` by
   the `did:key` it paired with — the one the credential is bound to — and the
   answer to that post is the credential itself (an SD-JWT VC with all its
   disclosures). It is taken once.

The issuer's tenant reads submitted applications, their files and the
holder's signature, and accepts or rejects them (``/tenants/{id}/applications``,
`routes.inbox`).

This module is the holder's side (start, answers, files, what is signed);
the offers are `routes.offers`, the wallet's requests and answers
`routes.application_wallet`.

Unsubmitted applications expire after a day.
"""

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

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
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import answers as checks
from registry_api import field_catalog as catalog
from registry_api import (
    throttle,
    wallet,
    webvh,
)
from registry_api.api.routes.auth import DbSession, as_utc
from registry_api.api.routes.offers import offer_view, offering
from registry_api.config import get_settings
from registry_api.errors import Refusal
from registry_api.models import (
    Application,
    ApplicationFile,
    Form,
    Issuer,
)
from registry_api.security import digest, new_token

router = APIRouter(prefix="/applications", tags=["applications"])

TTL = timedelta(days=1)
MAX_FILE = 10 * 1024 * 1024


def client_id() -> str:
    """Who the wallet answers: the catalog, where holders apply."""
    return get_settings().catalog_url.rstrip("/")


def _refuse(code: str, status_code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code, detail=code)


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
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`offer_not_found`"},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "`too_many_requests`: too many from the network address"
        },
    },
)
async def start(body: StartIn, request: Request, db: DbSession) -> Started:
    await throttle.hit(
        db,
        "too_many_requests",
        throttle.per_ip("applications", request, get_settings().ip_applications_per_hour),
    )
    issuer, form = await offering(db, body.issuer, body.type)
    now = datetime.now(UTC)
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


async def files_of(db: AsyncSession, item: Application) -> dict[str, dict[str, Any]]:
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


async def parts(db: AsyncSession, item: Application) -> tuple[Issuer, Form]:
    issuer = await db.get(Issuer, item.issuer_id)
    form = await db.get(Form, item.form_id)
    assert issuer is not None and form is not None
    return issuer, form


def filled_by_credentials(item: Application) -> dict[str, Any]:
    """The fields verified credentials filled, with their values."""
    values: dict[str, Any] = {}
    for result in item.presented:
        if result.get("verified"):
            values.update(result.get("fills", {}))
    return values


def deep_link(item: Application) -> str:
    uri = wallet.channel_url(f"/applications/{item.id}/request")
    return f"almena://auth?request_uri={quote(uri, safe='')}"


async def view(db: AsyncSession, item: Application) -> dict[str, Any]:
    issuer, form = await parts(db, item)
    live = item.wallet_expires_at is not None and as_utc(item.wallet_expires_at) > datetime.now(UTC)
    return {
        "id": str(item.id),
        "slug": item.slug,
        "status": item.status,
        "holder_did": item.holder_did,
        "offer": await offer_view(db, issuer, form, item.credential_type),
        "answers": item.answers,
        "filled": filled_by_credentials(item),
        "files": await files_of(db, item),
        "presented": item.presented,
        "wallet": {
            "purpose": item.wallet_purpose,
            "answered": item.wallet_answered,
            "live": live,
            "deep_link": deep_link(item) if live and not item.wallet_answered else None,
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
        **credential_status(item),
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
    return await view(db, item)


def paired(item: Application) -> None:
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
            "description": "`answers_invalid`, with `errors`: `{field: problem}`"
        },
    },
)
async def save_answers(item: Mine, body: AnswersIn, db: DbSession) -> dict[str, Any]:
    paired(item)
    _, form = await parts(db, item)
    kept, errors = checks.check(
        await checks.fields_of(db, form),
        body.answers,
        await files_of(db, item),
        set(filled_by_credentials(item)),
    )
    if errors:
        raise Refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "answers_invalid", errors=errors)
    item.answers = kept
    await db.commit()
    return await view(db, item)


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
    paired(item)
    _, form = await parts(db, item)
    found = {k: (stored, field) for k, stored, field in await checks.fields_of(db, form)}
    if key not in found or found[key][1].type != "file":
        raise _refuse("file_field_unknown", status.HTTP_422_UNPROCESSABLE_CONTENT)
    stored, field = found[key]
    allowed = catalog.media_types(
        field, stored.get("narrow", {}).get("values") or catalog.domain_values(field)
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
    paired(item)
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
        codes = catalog.codes_of(item)
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
    issuer, form = await parts(db, item)
    files = await files_of(db, item)
    filled = filled_by_credentials(item)
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


def check_signature(token: str, holder: str, nonce: str | None) -> dict[str, Any]:
    """The submission's JWS: by the holder's `did:key`, for the catalog and, while
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
            audience=client_id(),
            leeway=wallet.LEEWAY,
            options={"verify_exp": nonce is not None},
        )
    except (jwt.PyJWTError, wallet.WalletError):
        raise _refuse("invalid_signature", status.HTTP_400_BAD_REQUEST) from None
    if nonce is not None and claims.get("nonce") != nonce:
        raise _refuse("invalid_signature", status.HTTP_400_BAD_REQUEST)
    return claims


def credential_status(item: Application) -> dict[str, Any]:
    """An issued credential's status as its issuer's list says (as signed)."""
    return {
        "credential_status": item.credential_status,
        "credential_status_at": item.credential_status_at.isoformat()
        if item.credential_status_at
        else None,
    }
