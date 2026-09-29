"""Answers from an Almena wallet: a self-issued ID token, as SIOPv2 shapes it.

The portal shows a request (QR code or ``almena://auth?request_uri=…``); the
wallet fetches it, the person approves, and the wallet posts back
(``direct_post``) an ``id_token`` signed with the key it keeps for this
registry. The token is a JWS whose issuer and subject are that key's
``did:key`` (Ed25519), its audience the portal, and its nonce the request's.
No DIDComm and no key here: the API only checks signatures.
"""

import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Multicodec prefix of an Ed25519 public key (0xed, as a varint).
_ED25519 = b"\xed\x01"
# How far a token's `iat` may sit from this clock.
LEEWAY = 300


class WalletError(Exception):
    """A token that does not hold; `code` goes back to the wallet."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _ALPHABET.find(char)
        if index < 0:
            raise WalletError("invalid_did")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * zeros + body


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rest = divmod(number, 58)
        out = _ALPHABET[rest] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def did_key(public_key: bytes) -> str:
    """The `did:key` of a raw Ed25519 public key."""
    return "did:key:z" + b58encode(_ED25519 + public_key)


def public_key(did: str) -> Ed25519PublicKey:
    """The Ed25519 key a `did:key` names; anything else is refused."""
    if not did.startswith("did:key:z") or len(did) > 255:
        raise WalletError("invalid_did")
    raw = b58decode(did.removeprefix("did:key:z"))
    if not raw.startswith(_ED25519) or len(raw) != len(_ED25519) + 32:
        raise WalletError("invalid_did")
    return Ed25519PublicKey.from_public_bytes(raw[len(_ED25519) :])


def verify(id_token: str, *, audience: str, nonce: str) -> str:
    """The DID that signed `id_token` for this audience and nonce."""
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise WalletError("invalid_token") from None
    did = claims.get("sub")
    if not isinstance(did, str) or claims.get("iss") != did:
        raise WalletError("invalid_token")
    # The key id, when there is one, must be a key of that same DID.
    kid = header.get("kid")
    if kid is not None and (not isinstance(kid, str) or kid.split("#")[0] != did):
        raise WalletError("invalid_token")
    try:
        verified = jwt.decode(
            id_token,
            public_key(did),
            algorithms=["EdDSA"],
            audience=audience,
            leeway=LEEWAY,
            options={"require": ["iat", "exp", "nonce"]},
        )
    except jwt.PyJWTError:
        raise WalletError("invalid_token") from None
    if verified["nonce"] != nonce or abs(int(verified["iat"]) - time.time()) > LEEWAY:
        raise WalletError("invalid_token")
    return did
