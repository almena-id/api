"""Issuing a credential a holder applied for, as an SD-JWT VC.

Nobody signs on the server: the issuer's signer does, from their wallet, with
the key the issuer's DID lists (`assertionMethod`). The registry builds what is
to be signed and the wallet signs it as it is:

- the **header**: `EdDSA`, `typ: dc+sd-jwt`, `kid` the issuer's DID and the
  signer's key (``{did}#{multikey}``);
- the **payload**: `iss` (the issuer's DID), `iat`, `exp`, `vct` (the type's,
  from Almena's catalogue), `cnf.kid` — the `did:key` the holder applied with,
  so only that holder can present it — and `_sd`, a digest per claim;
- the **disclosures**: each claim, selectively disclosable, one by one
  (`[salt, name, value]`), whose digests the payload lists.

The wallet shows the claims (the disclosures, checked against `_sd`) before
signing; its JWS is checked here against the very header and payload sent, with
the signer's key, and the credential is the JWS followed by every disclosure.
The holder's wallet then takes it with proof of the key it is bound to
(`registry_api.api.routes.applications`, `receive`). The payload's `status`
names its entry in the issuer's status list, where it can later be suspended
or revoked (`registry_api.status_lists`).
"""

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import HTTPException, status

from registry_api import credential_catalog, wallet


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def disclosure(name: str, value: Any) -> str:
    """One claim, as an SD-JWT disclosure: `[salt, name, value]`, base64url."""
    raw = json.dumps([_b64(secrets.token_bytes(16)), name, value], ensure_ascii=False)
    return _b64(raw.encode())


def digest(disclosure_text: str) -> str:
    return _b64(hashlib.sha256(disclosure_text.encode("ascii")).digest())


def document(
    *,
    issuer: str,
    key: str,
    type_id: str,
    holder: str,
    claims: dict[str, Any],
    valid_until: datetime,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the issuer's signer signs: the header, the payload and the
    disclosures its `_sd` lists, in the order the claims came; with `status`,
    its entry in the issuer's status list (`registry_api.status_lists`)."""
    disclosures = [disclosure(name, value) for name, value in claims.items()]
    return {
        "header": {"alg": "EdDSA", "typ": "dc+sd-jwt", "kid": f"{issuer}#{key}"},
        "payload": {
            "iss": issuer,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int(valid_until.timestamp()),
            "vct": credential_catalog.vct(credential_catalog.BY_ID[type_id]),
            "cnf": {"kid": holder},
            "_sd_alg": "sha-256",
            "_sd": sorted(digest(text) for text in disclosures),
            **({"status": status} if status else {}),
        },
        "disclosures": disclosures,
    }


def claims_of(disclosures: list[str]) -> dict[str, Any]:
    """The claims the disclosures carry, by name."""
    found = {}
    for text in disclosures:
        _, name, value = json.loads(_b64decode(text))
        found[name] = value
    return found


def checked(document: dict[str, Any], jws: object, signers: list[str]) -> str:
    """The key that signed, if `jws` is one of `signers`' signature over the
    very header and payload of `document`. Anything else is refused."""
    refused = HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_signature")
    if not isinstance(jws, str) or jws.count(".") != 2:
        raise refused
    try:
        header = jwt.get_unverified_header(jws)
    except jwt.PyJWTError:
        raise refused from None
    key = str(header.get("kid", "")).rpartition("#")[2]
    if header != document["header"] or key not in signers:
        raise refused
    try:
        payload = jwt.decode(
            jws,
            wallet.public_key(f"did:key:{key}"),
            algorithms=["EdDSA"],
            options={"verify_exp": False, "verify_iat": False},
        )
    except (jwt.PyJWTError, wallet.WalletError):
        raise refused from None
    if payload != document["payload"]:
        raise refused
    return key


def signed(document: dict[str, Any], jws: object, signers: list[str]) -> tuple[str, str]:
    """The credential, if `jws` is the signer's signature over this header and
    payload (`checked`): the JWS and every disclosure, `~`-separated; and the
    key that signed."""
    key = checked(document, jws, signers)
    assert isinstance(jws, str)
    return jws + "~" + "".join(f"{text}~" for text in document["disclosures"]), key
