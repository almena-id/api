"""Signing in, or linking, with an Almena wallet (see `registry_api.wallet`).

1. The portal asks for a request (``POST /auth/wallet/requests``): a ``sign_in``,
   or a ``link`` for the signed-in account. It shows ``deep_link`` as a QR code
   and a link, and keeps ``poll`` to itself.
2. The wallet fetches ``request_uri`` and, once the person approves, posts an
   ``id_token`` to its ``response_uri`` (``direct_post``).
3. The portal polls ``…/result`` with ``poll`` until the answer is there: a
   session for a sign-in, a link result for a link. A wallet is a way in like
   a provider account (provider ``almena``, its ``did:key`` the subject); one
   seen for the first time signs up an account with no email, and a tenant of
   its own with no name.

The CLI asks the same way (``client: "cli"``): the wallet then answers the CLI
(``client_id`` ``{public_url}/cli``, on the API's own host, which vouches for
it) and the sheet names it. The key the wallet signs with comes from the API's
origin either way, so the portal and the CLI reach the same account.

The request's id travels in the QR code, so it is public: what holds a session
is the poll secret, which only the portal that asked ever sees.

**Signing.** Whoever signs as the tenant under its flow (``signing_flows``)
signs one of the tenant's identities' did:webvh log
entries the same way (``POST /tenants/{id}/identities/{id}/sign``): the
request carries the entry (``purpose: "sign"``), the wallet answers with a Data
Integrity proof by the key it keeps for this registry, and the registry writes
the entry to the log only if that key is one of the update keys in force and
the signer's own.
"""

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import (
    dids,
    issuance,
    messaging_keys,
    notices,
    queues,
    signing_flows,
    status_lists,
    wallet,
    webvh,
)
from registry_api.api.routes.account import LinkResult, settle_taken
from registry_api.api.routes.auth import (
    CurrentSession,
    DbSession,
    OptionalSession,
    SignedIn,
    as_utc,
    new_tenant,
    sign_in,
)
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.config import get_settings
from registry_api.mail import Locale
from registry_api.models import (
    Application,
    DidLogEntry,
    Identity,
    Issuer,
    Mediator,
    StatusList,
    Tenant,
    TenantMember,
    User,
    UserIdentity,
    Verifier,
    WalletRequest,
)
from registry_api.security import digest, new_token
from registry_api.vault import Vault, VaultError, get_vault

router = APIRouter(prefix="/auth/wallet", tags=["wallet"])
signing = APIRouter(prefix="/tenants/{tenant_id}", tags=["wallet"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]

PROVIDER = "almena"
# How long a request waits for a wallet.
TTL = timedelta(minutes=5)

# The identity of a tenant with no name, until the tenant is named.
_UNNAMED: dict[Locale, str] = {"en": "Unnamed tenant", "es": "Tenant sin nombre"}


# Who asks a wallet: the registry portal, or the `almena` CLI.
Client = Literal["portal", "cli"]


class RequestIn(BaseModel):
    purpose: Literal["sign_in", "link"] = "sign_in"
    locale: Locale = "en"
    client: Client = "portal"


class LocaleIn(BaseModel):
    # The portal's language.
    locale: Locale = "en"
    client: Client = "portal"


class RequestOut(BaseModel):
    id: uuid.UUID
    # Where the wallet reads the request; `deep_link` carries it for the QR code.
    request_uri: str
    deep_link: str
    # The portal's alone: it turns the answer into a session.
    poll: str
    expires_at: datetime


SignKind = Literal["did_log_entry", "endorsement", "credential", "status_list"]


class StatusChange(BaseModel):
    index: int
    status: Literal["valid", "suspended", "revoked"]
    # The credential's holder, as the issuer knows them.
    holder: str | None = None


class SignObject(BaseModel):
    """`sign`: what to sign, and what it is.

    `did_log_entry`: an identity's next did:webvh log entry, signed with a key
    named as a `did:key`. `credential`: a credential an issuer grants a holder,
    an SD-JWT VC (`document`: its header, payload and disclosures; see
    `registry_api.issuance`), signed as a JWS by the issuer's signer.
    `endorsement`: a tenant's membership credential for one of its issuers,
    verifiers or mediators (`document`) and, in the same approval, the
    component's `whois.vp` (`presentation`, whose `verifiableCredential` the
    wallet fills with the credential it has just signed), both with a key of
    the tenant's DID — for the presentation, as the component's controller.
    """

    kind: SignKind
    # What it is about: the identity's (or the item's) name, its tenant's
    # name, and the DID concerned (`null` for a first log entry).
    identity: str
    tenant: str | None
    did: str | None
    # `did_log_entry`: the version it makes.
    version: int | None = None
    # `endorsement`: until when it holds.
    valid_until: str | None = None
    # The multikeys that may sign it.
    signers: list[str]
    # The DID whose key signs, as `{verification_method}#{multikey}`; `null`
    # for a log entry, signed as `did:key:{multikey}#{multikey}`.
    verification_method: str | None = None
    proof_purpose: Literal["assertionMethod", "authentication"] = "assertionMethod"
    document: dict[str, Any]
    # `endorsement`: the presentation to sign after the credential, signed for
    # `authentication`.
    presentation: dict[str, Any] | None = None
    # `status_list`: the entry it changes, and to what (`valid`, `suspended`,
    # `revoked`); `null` when the list is signed as it is.
    status_change: StatusChange | None = None


class RequestObject(BaseModel):
    """What the wallet reads: who asks, for what, and where to answer."""

    # `id_token` for a sign-in or a link; `proof` for a signature.
    response_type: Literal["id_token", "proof"] = "id_token"
    response_mode: Literal["direct_post"] = "direct_post"
    # The portal or the CLI: the audience of the token, and what the wallet shows.
    client_id: str
    client_name: str
    response_uri: str
    nonce: str
    purpose: Literal["sign_in", "link", "sign"]
    expires_at: datetime
    sign: SignObject | None = None


class ResultIn(BaseModel):
    poll: str = Field(min_length=1, max_length=256)


class ResultOut(BaseModel):
    # `pending` until the wallet answers; then what came of it, once.
    status: Literal["pending", "signed_in", "linked", "taken", "signed"]
    session: SignedIn | None = None
    # With `taken`, when this account is empty: for `POST /auth/me/move`.
    move_ticket: str | None = None


def client_of(found: WalletRequest) -> tuple[str, str]:
    """The `client_id` and `client_name` a request names: whoever asked."""
    settings = get_settings()
    if found.client == "cli":
        return f"{settings.public_url.rstrip('/')}/cli", "Almena CLI"
    return settings.portal_url.rstrip("/"), "Almena Registry"


def _request_uri(request_id: uuid.UUID) -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/v1/auth/wallet/requests/{request_id}"


async def _live(db: AsyncSession, request_id: uuid.UUID) -> WalletRequest:
    found = await db.get(WalletRequest, request_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="request_not_found")
    if as_utc(found.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, detail="request_expired")
    return found


@router.post(
    "/requests",
    summary="Ask a wallet to sign in, or to link to the signed-in account",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "`not_authenticated` (link)"}},
)
async def create_request(body: RequestIn, session: OptionalSession, db: DbSession) -> RequestOut:
    if body.purpose == "link" and session is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = session.user_id if body.purpose == "link" and session else None
    return await _new_request(db, body.purpose, user_id, body.locale, body.client)


async def _new_request(
    db: AsyncSession,
    purpose: str,
    user_id: uuid.UUID | None,
    locale: str,
    client: Client,
    payload: str | None = None,
) -> RequestOut:
    now = datetime.now(UTC)
    # Old requests go as new ones come.
    await db.execute(delete(WalletRequest).where(WalletRequest.expires_at <= now))
    poll = new_token()
    found = WalletRequest(
        poll_hash=digest(poll),
        nonce=secrets.token_urlsafe(24),
        purpose=purpose,
        user_id=user_id,
        locale=locale,
        client=client,
        payload=payload,
        expires_at=now + TTL,
    )
    db.add(found)
    await db.commit()
    uri = _request_uri(found.id)
    return RequestOut(
        id=found.id,
        request_uri=uri,
        deep_link=f"almena://auth?request_uri={quote(uri, safe='')}",
        poll=poll,
        expires_at=found.expires_at,
    )


@router.get(
    "/requests/{request_id}",
    summary="The request, as the wallet reads it",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`request_not_found`"},
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def read_request(request_id: uuid.UUID, db: DbSession) -> RequestObject:
    found = await _live(db, request_id)
    sign = None
    if found.purpose == "sign":
        sign = SignObject.model_validate(json.loads(found.payload or "{}")["sign"])
    client_id, client_name = client_of(found)
    return RequestObject(
        response_type="proof" if sign else "id_token",
        client_id=client_id,
        client_name=client_name,
        response_uri=f"{_request_uri(found.id)}/response",
        nonce=found.nonce,
        purpose="sign" if sign else "link" if found.purpose == "link" else "sign_in",
        expires_at=found.expires_at,
        sign=sign,
    )


@router.post(
    "/requests/{request_id}/response",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="The wallet's answer: an `id_token` (form or JSON), or a `proof` (JSON)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "`invalid_token`, `invalid_did`, `invalid_proof`, "
            "`not_an_update_key`, `not_your_key`"
        },
        status.HTTP_404_NOT_FOUND: {"description": "`request_not_found`"},
        status.HTTP_409_CONFLICT: {"description": "`already_answered`"},
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def answer_request(
    request_id: uuid.UUID,
    request: Request,
    db: DbSession,
    vault: notices.VaultDep,
    courier: notices.Courier,
    broker: notices.BrokerDep,
) -> None:
    found = await _live(db, request_id)
    if found.did is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already_answered")
    # `direct_post` is a form; JSON is taken too.
    raw = (await request.body())[:65536].decode(errors="replace")
    token: object = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = json.loads(raw)
            if found.purpose == "sign":
                await _sign(db, found, body, notices.Notifier(vault, courier, broker))
                return
            token = body.get("id_token")
        except (ValueError, AttributeError):
            token = None
    elif found.purpose == "sign":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_proof")
    else:
        values = parse_qs(raw).get("id_token")
        token = values[0] if values else None
    if not isinstance(token, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    try:
        did = wallet.verify(token, audience=client_of(found)[0], nonce=found.nonce)
    except wallet.WalletError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error.code) from None
    found.did = did
    await db.commit()


async def _sign(
    db: AsyncSession, found: WalletRequest, body: dict[str, Any], notifier: notices.Notifier
) -> None:
    """Checks the wallet's proof over what it was asked to sign, and keeps the
    result: a log entry, or an endorsed (and so published) component."""
    payload = json.loads(found.payload or "{}")
    sign = SignObject.model_validate(payload["sign"])
    if sign.kind == "credential":
        await _issue(db, found, payload, sign, body.get("jws"), notifier)
        return
    if sign.kind == "status_list":
        await _set_statuses(db, found, payload, sign, body.get("jws"), notifier)
        return
    proof = body.get("proof")
    try:
        did = webvh.check_proof(
            sign.document,
            proof,
            sign.signers,
            controller=sign.verification_method,
            purpose=sign.proof_purpose,
            refusal="not_an_update_key" if sign.kind == "did_log_entry" else "not_a_signer",
        )
    except webvh.LogError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error.code) from None
    # The key is the person's who asked, not merely any of the signers.
    if webvh.multikey(did) not in await my_keys(db, found.user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="not_your_key")
    assert isinstance(proof, dict)
    signed_document = {**sign.document, "proof": proof}
    if sign.kind == "endorsement":
        await _endorse(db, payload, sign, signed_document, body.get("presentation_proof"), did)
    else:
        await _append(db, uuid.UUID(payload["identity_id"]), payload["previous"], sign, proof)
    found.did = did
    await db.commit()


async def _issue(
    db: AsyncSession,
    found: WalletRequest,
    payload: dict[str, Any],
    sign: SignObject,
    jws: object,
    notifier: notices.Notifier,
) -> None:
    """The issuer's signer signed the credential: if it is their key, over
    what was sent, the application's credential is issued, and the holder
    told so over DIDComm when its wallet said where."""
    credential, key = issuance.signed(sign.document, jws, sign.signers)
    if key not in await my_keys(db, found.user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="not_your_key")
    item = await db.get(Application, uuid.UUID(payload["application_id"]))
    if item is None or item.status != "accepted":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="application_gone")
    item.credential = credential
    item.status = "issued"
    item.issued_at = datetime.now(UTC)
    item.credential_status = "valid"
    item.credential_status_at = item.issued_at
    found.did = f"did:key:{key}"
    await db.commit()
    await notifier.notify(db, item, "issued")
    await queues.issued(db, notifier.broker, item)


async def _set_statuses(
    db: AsyncSession,
    found: WalletRequest,
    payload: dict[str, Any],
    sign: SignObject,
    jws: object,
    notifier: notices.Notifier,
) -> None:
    """The issuer's signer signed its status list: if it is their key, over
    what was sent, and nothing replaced the list meanwhile, it is the list
    from now on — and the credential whose status it changes has that status,
    which the issuer's queue hears of."""
    key = issuance.checked(sign.document, jws, sign.signers)
    if key not in await my_keys(db, found.user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="not_your_key")
    item = await db.get(StatusList, uuid.UUID(payload["status_list_id"]))
    if item is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="status_list_gone")
    if item.revision != payload["revision"]:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="status_list_changed")
    now = datetime.now(UTC)
    item.statuses = status_lists.decode(sign.document["payload"]["status_list"]["lst"])
    item.token = cast(str, jws)
    item.revision += 1
    item.signed_at = now
    changed: Application | None = None
    if payload.get("application_id"):
        application = await db.get(Application, uuid.UUID(payload["application_id"]))
        if application is not None and application.status_list_id == item.id:
            application.credential_status = payload["credential_status"]
            application.credential_status_at = now
            changed = application
    found.did = f"did:key:{key}"
    await db.commit()
    if changed is not None:
        await queues.status_changed(db, notifier.broker, changed)


async def _append(
    db: AsyncSession,
    identity_id: uuid.UUID,
    previous: str | None,
    sign: SignObject,
    proof: dict[str, Any],
) -> None:
    entry: webvh.Entry = sign.document
    identity = await db.get_one(Identity, identity_id)
    # The log must still end where the entry was built from.
    entries = await dids.log(db, identity.id)
    head = entries[-1]["versionId"] if entries else None
    if head != previous:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="log_moved")
    db.add(
        DidLogEntry(
            identity_id=identity.id,
            version=len(entries) + 1,
            entry=json.dumps(webvh.signed(entry, proof), separators=(",", ":")),
        )
    )
    if identity.did is None:
        identity.did = str(entry["state"]["id"])


async def _endorse(
    db: AsyncSession,
    payload: dict[str, Any],
    sign: SignObject,
    signed_credential: dict[str, Any],
    proof: object,
    did: str,
) -> None:
    """The tenant endorsed a component: its presentation must hold too, by the
    same key; then it is the component's `whois.vp`, and the component is
    published."""
    assert sign.presentation is not None
    presentation = {**sign.presentation, "verifiableCredential": [signed_credential]}
    try:
        presenter = webvh.check_proof(
            presentation,
            proof,
            sign.signers,
            controller=sign.verification_method,
            purpose="authentication",
            refusal="not_a_signer",
        )
    except webvh.LogError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error.code) from None
    if presenter != did:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_proof")
    models: dict[str, type[Issuer | Verifier | Mediator]] = {
        "issuers": Issuer,
        "verifiers": Verifier,
        "mediators": Mediator,
    }
    item = cast(
        Issuer | Verifier | Mediator | None,
        await db.get(models[payload["kind"]], uuid.UUID(payload["item_id"])),
    )
    if item is None or str(item.identity_id) != payload["identity_id"]:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="item_gone")
    identity = await db.get_one(Identity, item.identity_id)
    identity.presentation = json.dumps({**presentation, "proof": proof}, separators=(",", ":"))
    identity.endorsed_until = datetime.fromisoformat(signed_credential["validUntil"])
    if item.published_at is None:
        item.published_at = datetime.now(UTC)


async def _account_for(db: AsyncSession, did: str, locale: Locale) -> User:
    """The account the wallet signs in to, signed up (no email) the first time."""
    linked = await db.scalar(
        select(UserIdentity).where(UserIdentity.provider == PROVIDER, UserIdentity.subject == did)
    )
    if linked is not None:
        return await db.get_one(User, linked.user_id)
    user = User(email=None)
    db.add(user)
    await db.flush()
    tenant = await new_tenant(db, None, _UNNAMED[locale])
    db.add(TenantMember(tenant_id=tenant.id, user_id=user.id, role="admin"))
    db.add(UserIdentity(user_id=user.id, provider=PROVIDER, subject=did, email=None))
    await db.flush()
    return user


@router.post(
    "/requests/{request_id}/result",
    summary="What came of a request, for the portal that asked (poll until not `pending`)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`wrong_poll`, or another account's link"},
        status.HTTP_404_NOT_FOUND: {"description": "`request_not_found`"},
        status.HTTP_410_GONE: {"description": "`request_expired`"},
    },
)
async def request_result(
    request_id: uuid.UUID, body: ResultIn, session: OptionalSession, db: DbSession
) -> ResultOut:
    found = await _live(db, request_id)
    if not secrets.compare_digest(found.poll_hash, digest(body.poll)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="wrong_poll")
    if found.purpose in ("link", "sign") and (session is None or session.user_id != found.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="wrong_poll")
    did = found.did
    if did is None:
        return ResultOut(status="pending")
    # An answer is good once.
    locale: Locale = "es" if found.locale == "es" else "en"
    await db.delete(found)
    if found.purpose == "sign":
        await db.commit()
        return ResultOut(status="signed")

    if session is None or found.purpose == "sign_in":
        return ResultOut(
            status="signed_in", session=await sign_in(db, await _account_for(db, did, locale))
        )

    owner = await db.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.provider == PROVIDER, UserIdentity.subject == did
        )
    )
    result: LinkResult
    if owner is not None:
        result = await settle_taken(db, session.user, owner)
    else:
        db.add(UserIdentity(user_id=session.user_id, provider=PROVIDER, subject=did))
        await db.commit()
        result = LinkResult(status="linked")
    return ResultOut(status=result.status, move_ticket=result.move_ticket)


@signing.post(
    "/identities/{identity_id}/sign",
    summary="Ask the signer's wallet to sign the identity's next log entry "
    "(whoever signs as the tenant under its flow)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "`not_a_signer`"},
        status.HTTP_404_NOT_FOUND: {"description": "`identity_not_found`"},
        status.HTTP_409_CONFLICT: {
            "description": "`up_to_date`: nothing to sign; `no_signers`: no signer has a wallet"
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "`vault_unavailable`: an issuer's or verifier's messaging key "
            "could not be made"
        },
    },
)
async def sign_identity(
    tenant_id: TenantId,
    identity_id: uuid.UUID,
    body: LocaleIn,
    session: CurrentSession,
    db: DbSession,
    vault: Annotated[Vault, Depends(get_vault)],
) -> RequestOut:
    identity = await db.get(Identity, identity_id)
    if identity is None or identity.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="identity_not_found")
    if not await signing_flows.signs(db, await db.get_one(Tenant, tenant_id), session.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_a_signer")
    # Signing an issuer or a verifier is when its messaging key is made, so
    # the entry signed now is the one that lists it.
    item = await messaging_keys.owner(db, identity.id)
    if item is not None and identity.agreement_key is None:
        try:
            await messaging_keys.ensure(vault, item, identity)
        except VaultError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_unavailable"
            ) from None
        await db.commit()
    try:
        entry = await dids.next_entry(db, get_settings().did_url, identity)
    except webvh.LogError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=error.code) from None
    if entry is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="up_to_date")
    entries = await dids.log(db, identity.id)
    signers = webvh.signers_for(entries, entry)
    if not set(await my_keys(db, session.user_id)) & set(signers):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not_a_signer")
    tenant = await db.get_one(Tenant, identity.tenant_id)
    sign = SignObject(
        kind="did_log_entry",
        identity=identity.name,
        tenant=tenant.name,
        did=identity.did,
        version=int(str(entry["versionId"]).split("-")[0]),
        signers=signers,
        document=entry,
    )
    extra = {
        "identity_id": str(identity.id),
        "previous": entries[-1]["versionId"] if entries else None,
    }
    return await new_sign_request(db, session.user_id, body, sign, extra)


async def my_keys(db: AsyncSession, user_id: uuid.UUID | None) -> list[str]:
    """The multikeys of the wallets linked to an account."""
    subjects = await db.scalars(
        select(UserIdentity.subject).where(
            UserIdentity.user_id == user_id, UserIdentity.provider == PROVIDER
        )
    )
    return [webvh.multikey(subject) for subject in subjects]


async def new_sign_request(
    db: AsyncSession,
    user_id: uuid.UUID,
    asked: LocaleIn,
    sign: SignObject,
    extra: dict[str, Any],
) -> RequestOut:
    """A request for `user_id`'s wallet to sign `sign`, asked by the portal or
    the CLI in `asked`; `extra` is kept for when the answer comes (what it
    belongs to)."""
    payload = {"sign": sign.model_dump(), **extra}
    return await _new_request(db, "sign", user_id, asked.locale, asked.client, json.dumps(payload))
