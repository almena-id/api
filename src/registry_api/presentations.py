"""Verifying the credentials a holder presents for a form.

A form's `credentials` block (`registry_api.form_credentials`) asks for
credentials with an OpenID4VP DCQL query; the holder answers with a
`vp_token`: for each credential query id, the presentations made for it. Each
presentation is checked, all of it, before any of its claims is believed:

- **format**: an SD-JWT VC with its key binding (`dc+sd-jwt`), or a W3C
  credential inside a holder-signed presentation, both as JWTs (`jwt_vc_json`,
  the credential's claims under `vc` or, VCDM 2.0, at the top);
- **issuer**: the signature, with a key its DID lists under `assertionMethod`.
  Only the registry's DIDs (their `did:webvh`, or the `did:web` alias) are
  resolved — and those are the only issuers a form may trust, so nothing is
  fetched to know them; a draft's DID does not resolve;
- **trust**: as the form says — `registry`, a published issuer that declares it
  grants the type; `issuers`, one of those named. `framework` (the EU PID,
  trusted through the EU's lists and X.509) is not verified yet: such a
  presentation is reported, never believed;
- **type**: the `vct`, or the W3C `type`, is the catalogue's;
- **validity**: `nbf`/`exp` (`validFrom`/`validUntil`), with a little leeway;
- **status**: a Token Status List (`status.status_list`) or a W3C Bitstring
  Status List (`credentialStatus`), fetched and checked when the credential has
  one; a list that cannot be fetched or checked fails it;
- **holder binding**: the key-binding JWT (`kb+jwt`) or the presentation is
  signed by the holder's key — the credential's `cnf` or its subject's
  `did:key` — for this `nonce` and `audience`, over these disclosures;
- **claims**: SD-JWT disclosures each match a digest, once; every claim the
  form asks for that the type always carries is there (optional ones are
  passed on when present).

Keys are Ed25519 (`EdDSA`), the wallets' and the registry's. The result names
each problem by a code; nothing is trusted on a partial check.
"""

import base64
import gzip
import hashlib
import json
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import credential_catalog as catalog
from registry_api import dids, trust_anchor, wallet
from registry_api.config import get_settings
from registry_api.models import Identity, Issuer

LEEWAY = 300

# Fetches a status list: its URL in, its body out.
StatusFetch = Callable[[str], Awaitable[str]]


async def fetch_status(url: str) -> str:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            url, headers={"Accept": "application/statuslist+jwt, application/vc+jwt, */*"}
        )
        response.raise_for_status()
        return response.text


def get_status_fetch() -> StatusFetch:
    return fetch_status


class Problem(Exception):
    """Why a presentation is not believed; `code` goes into the result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Checked:
    """One credential of the form, as presented."""

    key: str
    presented: bool = False
    verified: bool = False
    query: str | None = None
    format: str | None = None
    issuer: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return jwt.get_unverified_header(token), jwt.decode(
            token, options={"verify_signature": False}
        )
    except jwt.PyJWTError:
        raise Problem("format_invalid") from None


def _verify(token: str, key: Ed25519PublicKey, **options: Any) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            leeway=LEEWAY,
            options={"verify_aud": "audience" in options, "require": []},
            **options,
        )
        return claims
    except jwt.ExpiredSignatureError:
        raise Problem("expired") from None
    except jwt.ImmatureSignatureError:
        raise Problem("not_yet_valid") from None
    except jwt.InvalidAudienceError:
        raise Problem("audience_mismatch") from None
    except jwt.PyJWTError:
        raise Problem("signature_invalid") from None


# Issuers ---------------------------------------------------------------------


def _did_web_prefix() -> str:
    parts = urlsplit(get_settings().did_url)
    path = [quote(p, safe="") for p in parts.path.split("/") if p]
    return ":".join(["did:web", quote(parts.netloc, safe=""), *path, dids.PATH]) + ":"


async def registry_identity(db: AsyncSession, did: str) -> Identity | None:
    """The registry identity a DID names: its `did:webvh`, or its `did:web` alias."""
    found = await db.scalar(select(Identity).where(Identity.did == did))
    prefix = _did_web_prefix()
    if found is None and did.startswith(prefix):
        found = await db.scalar(select(Identity).where(Identity.slug == did.removeprefix(prefix)))
        if found is not None and found.did is None:
            found = None
    return found


async def issuer_keys(db: AsyncSession, did: str) -> list[tuple[str, Ed25519PublicKey]]:
    """The keys a registry DID signs credentials with (`assertionMethod`), by
    their fragment; none for a DID that does not resolve."""
    identity = await registry_identity(db, did)
    if identity is None or await dids.is_draft(db, identity.id):
        return []
    document = await dids.published(db, identity)
    keys = []
    for key in dids.keys_under(document, "assertionMethod"):
        try:
            keys.append((key, wallet.public_key(f"did:key:{key}")))
        except wallet.WalletError:
            continue
    return keys


async def _signed_by_issuer(db: AsyncSession, token: str, did: str) -> dict[str, Any]:
    header, _ = _unverified(token)
    keys = await issuer_keys(db, did)
    if not keys:
        raise Problem("issuer_unresolvable")
    fragment = str(header.get("kid", "")).rpartition("#")[2]
    chosen = [key for name, key in keys if name == fragment] or [key for _, key in keys]
    last = Problem("signature_invalid")
    for key in chosen:
        try:
            return _verify(token, key)
        except Problem as problem:
            if problem.code != "signature_invalid":
                raise
            last = problem
    raise last


async def _trusted(db: AsyncSession, entry: dict[str, Any], did: str, tenant_id: uuid.UUID) -> None:
    """A published issuer granting the type — a tenant's own type: one of that
    tenant's issuers —, and, under `issuers`, one of those named."""
    if entry["trust"] == "framework":
        raise Problem("trust_framework_unsupported")
    identity = await registry_identity(db, did)
    issuer = (
        await db.scalar(
            select(Issuer).where(
                Issuer.identity_id == identity.id, Issuer.published_at.is_not(None)
            )
        )
        if identity
        else None
    )
    if issuer is None or entry["type"] not in (issuer.credential_types or []):
        raise Problem("issuer_untrusted")
    if entry["type"].startswith(catalog.PREFIX) and issuer.tenant_id != tenant_id:
        # Another tenant's type of the same name is not this one.
        raise Problem("issuer_untrusted")
    if (
        entry["trust"] == "issuers"
        and identity is not None
        and identity.did not in entry.get("issuers", [])
    ):
        raise Problem("issuer_untrusted")


# Status ----------------------------------------------------------------------


async def _token_status(
    db: AsyncSession, status: dict[str, Any], issuer: str, fetch: StatusFetch
) -> None:
    """IETF Token Status List: the entry's bits at `idx` must be 0 (valid)."""
    try:
        reference = status["status_list"]
        index = int(reference["idx"])
        token = (await fetch(str(reference["uri"]))).strip()
        claims = await _signed_by_issuer(db, token, issuer)
        listed = claims["status_list"]
        bits = int(listed["bits"])
        data = zlib.decompress(_b64decode(listed["lst"]))
        per_byte = 8 // bits
        byte = data[index // per_byte]
        value = (byte >> ((index % per_byte) * bits)) & ((1 << bits) - 1)
    except Problem:
        raise Problem("status_unverifiable") from None
    except Exception:
        raise Problem("status_unverifiable") from None
    if value != 0:
        raise Problem("revoked" if value == 1 else "suspended")


async def _bitstring_status(
    db: AsyncSession, status: dict[str, Any], issuer: str, fetch: StatusFetch
) -> None:
    """W3C Bitstring Status List: its list credential, as a JWT by the same issuer."""
    try:
        index = int(status["statusListIndex"])
        token = (await fetch(str(status["statusListCredential"]))).strip()
        claims = await _signed_by_issuer(db, token, issuer)
        listed = claims.get("vc", claims)["credentialSubject"]
        encoded = str(listed["encodedList"]).removeprefix("u")
        data = gzip.decompress(_b64decode(encoded))
        value = (data[index // 8] >> (7 - index % 8)) & 1
    except Exception:
        raise Problem("status_unverifiable") from None
    if value:
        raise Problem("revoked" if status.get("statusPurpose") != "suspension" else "suspended")


# SD-JWT VC -------------------------------------------------------------------


def _digest(disclosure: str) -> str:
    return _b64encode(hashlib.sha256(disclosure.encode("ascii")).digest())


def _disclose(payload: dict[str, Any], disclosures: list[str]) -> dict[str, Any]:
    """The payload with its disclosed claims in place, each disclosure used once."""
    by_digest: dict[str, list[Any]] = {}
    for disclosure in disclosures:
        try:
            decoded = json.loads(_b64decode(disclosure))
        except ValueError:
            raise Problem("disclosure_invalid") from None
        if not isinstance(decoded, list) or len(decoded) not in (2, 3):
            raise Problem("disclosure_invalid")
        digest = _digest(disclosure)
        if digest in by_digest:
            raise Problem("disclosure_invalid")
        by_digest[digest] = decoded
    used: set[str] = set()

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            out = {k: walk(v) for k, v in value.items() if k not in ("_sd", "_sd_alg")}
            for digest in value.get("_sd", []):
                found = by_digest.get(digest)
                if found is None:
                    continue
                if len(found) != 3 or digest in used or found[1] in out:
                    raise Problem("disclosure_invalid")
                used.add(digest)
                out[found[1]] = walk(found[2])
            return out
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict) and set(item) == {"..."}:
                    found = by_digest.get(item["..."])
                    if found is None:
                        continue
                    if len(found) != 2 or item["..."] in used:
                        raise Problem("disclosure_invalid")
                    used.add(item["..."])
                    items.append(walk(found[1]))
                else:
                    items.append(walk(item))
            return items
        return value

    disclosed: dict[str, Any] = walk(payload)
    if used != set(by_digest):
        raise Problem("disclosure_invalid")
    return disclosed


def _holder_key(cnf: Any) -> Ed25519PublicKey:
    """The holder's key a credential is bound to: `cnf.jwk` (OKP Ed25519) or a
    `did:key` as `cnf.kid`."""
    try:
        if isinstance(cnf, dict) and "jwk" in cnf:
            key = jwt.PyJWK(cnf["jwk"], algorithm="EdDSA").key
            if isinstance(key, Ed25519PublicKey):
                return key
        if isinstance(cnf, dict) and str(cnf.get("kid", "")).startswith("did:key:"):
            return wallet.public_key(str(cnf["kid"]).partition("#")[0])
    except (jwt.PyJWTError, wallet.WalletError, ValueError, TypeError):
        pass
    raise Problem("holder_binding_invalid")


async def _sd_jwt(
    db: AsyncSession,
    presentation: str,
    entry: dict[str, Any],
    *,
    item: catalog.CredentialType,
    tenant_id: uuid.UUID,
    nonce: str,
    audience: str,
    fetch: StatusFetch,
) -> tuple[str, dict[str, Any]]:
    parts = presentation.split("~")
    if len(parts) < 2 or not parts[-1]:
        # No key-binding JWT: nothing ties it to whoever presents it.
        raise Problem("holder_binding_invalid")
    issued, disclosures, binding = parts[0], parts[1:-1], parts[-1]
    _, unverified = _unverified(issued)
    issuer = str(unverified.get("iss", ""))
    if unverified.get("_sd_alg", "sha-256") != "sha-256":
        raise Problem("format_invalid")
    if unverified.get("vct") != catalog.vct(item):
        raise Problem("type_mismatch")
    await _trusted(db, entry, issuer, tenant_id)
    payload = await _signed_by_issuer(db, issued, issuer)

    header, _ = _unverified(binding)
    if header.get("typ") != "kb+jwt":
        raise Problem("holder_binding_invalid")
    try:
        bound = _verify(binding, _holder_key(payload.get("cnf")), audience=audience)
    except Problem as problem:
        raise Problem(
            "audience_mismatch" if problem.code == "audience_mismatch" else "holder_binding_invalid"
        ) from None
    if bound.get("nonce") != nonce:
        raise Problem("nonce_mismatch")
    sd_hash = _b64encode(hashlib.sha256("~".join(parts[:-1]).encode("ascii") + b"~").digest())
    if bound.get("sd_hash") != sd_hash or abs(time.time() - float(bound.get("iat", 0))) > LEEWAY:
        raise Problem("holder_binding_invalid")

    claims = _disclose(payload, disclosures)
    if isinstance(payload.get("status"), dict):
        await _token_status(db, payload["status"], issuer, fetch)
    return issuer, claims


# W3C, as JWTs ----------------------------------------------------------------


async def _w3c(
    db: AsyncSession,
    presentation: str,
    entry: dict[str, Any],
    *,
    item: catalog.CredentialType,
    tenant_id: uuid.UUID,
    nonce: str,
    audience: str,
    fetch: StatusFetch,
) -> tuple[str, dict[str, Any]]:
    _, outer = _unverified(presentation)
    holder = str(outer.get("iss") or outer.get("vp", {}).get("holder", ""))
    try:
        holder_key = wallet.public_key(holder)
    except wallet.WalletError:
        raise Problem("holder_binding_invalid") from None
    vp = _verify(presentation, holder_key, audience=audience)
    if vp.get("nonce") != nonce:
        raise Problem("nonce_mismatch")
    credentials = vp.get("vp", {}).get("verifiableCredential", [])
    if not isinstance(credentials, list) or len(credentials) != 1:
        raise Problem("format_invalid")
    token = credentials[0]
    if not isinstance(token, str):
        raise Problem("format_invalid")
    _, unverified = _unverified(token)
    body = unverified.get("vc", unverified)
    issuer = unverified.get("iss") or body.get("issuer")
    issuer = str(issuer.get("id") if isinstance(issuer, dict) else issuer or "")
    if item.w3c_type not in body.get("type", []):
        raise Problem("type_mismatch")
    await _trusted(db, entry, issuer, tenant_id)
    signed = await _signed_by_issuer(db, token, issuer)
    body = signed.get("vc", signed)
    now = time.time()
    for bound, late in (("validFrom", False), ("validUntil", True)):
        if bound in body:
            moment = _timestamp(str(body[bound]))
            if late and now > moment + LEEWAY:
                raise Problem("expired")
            if not late and now < moment - LEEWAY:
                raise Problem("not_yet_valid")
    subject = body.get("credentialSubject", {})
    if not isinstance(subject, dict) or subject.get("id", signed.get("sub")) != holder:
        raise Problem("holder_binding_invalid")
    status = body.get("credentialStatus")
    if isinstance(status, dict):
        await _bitstring_status(db, status, issuer, fetch)
    return issuer, {k: v for k, v in subject.items() if k != "id"}


def _timestamp(text: str) -> float:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise Problem("format_invalid") from None


# The form's vp_token ---------------------------------------------------------

VERIFIERS = {"sd_jwt": _sd_jwt, "w3c": _w3c}
FORMATS = {"sd_jwt": "dc+sd-jwt", "w3c": "jwt_vc_json"}


async def verify(
    db: AsyncSession,
    credentials: list[dict[str, Any]],
    vp_token: dict[str, list[str]],
    *,
    tenant_id: uuid.UUID,
    nonce: str,
    audience: str,
    fetch: StatusFetch,
) -> list[Checked]:
    """Each credential the form asks for, as presented in `vp_token` (keyed by
    the form's DCQL query ids): verified, or why not. `tenant_id`, the form's:
    whose own types it may ask for."""
    types = (await trust_anchor.load(db, tenant_id)).types
    known = {
        f"{entry['key']}_{suffix}": (entry, suffix) for entry in credentials for suffix in VERIFIERS
    }
    results = {entry["key"]: Checked(key=entry["key"]) for entry in credentials}
    for query, presentations in vp_token.items():
        if query not in known:
            continue
        entry, suffix = known[query]
        result = results[entry["key"]]
        if result.presented or not presentations:
            continue
        result.presented, result.query, result.format = True, query, FORMATS[suffix]
        try:
            item = types.get(entry["type"])
            if item is None:
                raise Problem("type_mismatch")
            issuer, claims = await VERIFIERS[suffix](
                db,
                presentations[0],
                entry,
                item=item,
                tenant_id=tenant_id,
                nonce=nonce,
                audience=audience,
                fetch=fetch,
            )
            result.issuer = issuer
            # The type's always-present claims must be there; its optional ones,
            # when asked for, are passed on if the credential has them.
            always = {c.name for c in item.claims if c.required}
            if any(claim in always and claim not in claims for claim in entry["claims"]):
                raise Problem("claims_missing")
            result.claims = {claim: claims[claim] for claim in entry["claims"] if claim in claims}
            result.verified = True
        except Problem as problem:
            result.problems.append(problem.code)
    return list(results.values())
