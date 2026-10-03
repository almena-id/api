"""DIDComm v2, as much of it as an issuer needs to write to a holder.

A holder's wallet pairs with an issuer (`routes.applications`, QR 1) and
names, besides the key it signs with, the DID it receives messages at: a
`did:peer:2` of its own for that issuer, whose `DIDCommMessaging` service is
its mediator's `did:web`. The issuer writes to it from its own DID, with the
X25519 key the vault keeps for it (`messaging_keys`):

1. the message is **authcrypted** (`ECDH-1PU+A256KW`, `A256CBC-HS512`) from
   the issuer's `keyAgreement` key to the holder's;
2. wrapped in a Routing 2.0 **`forward`** whose `next` is the holder's DID,
   **anoncrypted** (`ECDH-ES+A256KW`, `A256CBC-HS512`) to the mediator's
   X25519 `keyAgreement` keys;
3. **posted** to the mediator's HTTP(S) DIDComm endpoint, which queues it
   until the wallet picks it up.

Byte for byte what `almena-didcomm` (the mediator's and the wallet's library)
packs and unpacks: the protected header carries `epk`, `apv` (SHA-256 of the
sorted recipient kids joined with `.`) and, for authcrypt, `skid` and `apu`;
the Concat KDF of ECDH-1PU binds the content tag. Only X25519: the curve of
every key here. Nothing is received: the issuer's mailbox is not read yet.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from registry_api.wallet import WalletError, b58decode

ENCRYPTED = "application/didcomm-encrypted+json"
PLAIN = "application/didcomm-plain+json"
FORWARD = "https://didcomm.org/routing/2.0/forward"
# Multicodec prefixes, as multikeys start: x25519-pub (`z6LS…`), ed25519-pub.
_X25519 = b"\xec\x01"
_ED25519 = b"\xed\x01"
# A did:peer:2 is a few keys and a service: anything longer is not one.
MAX_DID = 2048


class DidCommError(Exception):
    """What could not be done; `code` says what."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def x25519_of(multibase: str) -> bytes:
    """The raw X25519 public key a multikey (`z6LS…`) names."""
    try:
        raw = b58decode(multibase.removeprefix("z")) if multibase.startswith("z") else b""
    except WalletError:
        raw = b""
    if not raw.startswith(_X25519) or len(raw) != len(_X25519) + 32:
        raise DidCommError("invalid_key")
    return raw[len(_X25519) :]


# DIDs -------------------------------------------------------------------------


@dataclass(frozen=True)
class Peer:
    """What a `did:peer:2` says that matters to write to it."""

    did: str
    # Its key agreement key (`{did}#key-N`) and its raw X25519 public key.
    kid: str
    key: bytes
    # Where it receives: its mediator's DID (or a transport URI).
    service: str


_LONG = {"t": "type", "s": "serviceEndpoint", "r": "routingKeys", "a": "accept"}


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        return {_LONG.get(k, k): _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return "DIDCommMessaging" if value == "dm" else value


def _endpoint_uri(service: dict[str, Any]) -> str | None:
    """The first `didcomm/v2` URI of a `DIDCommMessaging` service."""
    if service.get("type") != "DIDCommMessaging":
        return None
    endpoints = service.get("serviceEndpoint")
    for endpoint in endpoints if isinstance(endpoints, list) else [endpoints]:
        if isinstance(endpoint, str):
            return endpoint
        if isinstance(endpoint, dict) and isinstance(endpoint.get("uri"), str):
            accept = endpoint.get("accept") or ["didcomm/v2"]
            if "didcomm/v2" in accept:
                return str(endpoint["uri"])
    return None


def peer(did: str) -> Peer:
    """A `did:peer:2` read: keys are `#key-1`, `#key-2`… in order; the first
    `E` (X25519) is the one to encrypt to, the first DIDComm service where it
    receives. Anything else is refused (`invalid_didcomm`)."""
    if not did.startswith("did:peer:2.") or len(did) > MAX_DID:
        raise DidCommError("invalid_didcomm")
    kid: str | None = None
    key = b""
    service: str | None = None
    count = 0
    for element in did.removeprefix("did:peer:2.").split("."):
        code, value = element[:1], element[1:]
        if code == "S":
            try:
                found = _expand(json.loads(unb64(value.rstrip("="))))
            except ValueError:
                raise DidCommError("invalid_didcomm") from None
            if service is None and isinstance(found, dict):
                service = _endpoint_uri(found)
            continue
        if code not in "AEVID" or not code:
            raise DidCommError("invalid_didcomm")
        count += 1
        if code == "E" and kid is None:
            try:
                key = x25519_of(value)
            except DidCommError:
                continue
            kid = f"{did}#key-{count}"
    if kid is None or service is None:
        raise DidCommError("invalid_didcomm")
    return Peer(did=did, kid=kid, key=key, service=service)


def did_web_url(did: str) -> str:
    """Where a `did:web` document is: `/.well-known/did.json` for a bare host,
    `/{path}/did.json` otherwise; a port is written `%3A`."""
    if not did.startswith("did:web:"):
        raise DidCommError("mediator_unresolvable")
    host, *path = did.removeprefix("did:web:").split(":")
    host = unquote(host)
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    where = "/".join(quote(unquote(part), safe="") for part in path)
    return (
        f"{scheme}://{host}/{where}/did.json" if path else f"{scheme}://{host}/.well-known/did.json"
    )


@dataclass(frozen=True)
class Mediator:
    did: str
    # Its X25519 keyAgreement keys: (kid, raw public key).
    keys: list[tuple[str, bytes]]
    # Where to post: its first HTTP(S) DIDComm endpoint.
    endpoint: str


def _absolute(did: str, ref: str) -> str:
    return f"{did}{ref}" if ref.startswith("#") else ref


def mediator(document: dict[str, Any]) -> Mediator:
    """A mediator's DID document read: its X25519 keyAgreement keys and the
    HTTP(S) endpoint of its DIDComm service."""
    did = str(document.get("id", ""))
    methods = {
        _absolute(did, str(m.get("id", ""))): m
        for m in document.get("verificationMethod", [])
        if isinstance(m, dict)
    }
    keys: list[tuple[str, bytes]] = []
    for ref in document.get("keyAgreement", []):
        method = methods.get(_absolute(did, ref)) if isinstance(ref, str) else ref
        if not isinstance(method, dict):
            continue
        kid = _absolute(did, str(method.get("id", ref)))
        jwk = method.get("publicKeyJwk")
        try:
            if isinstance(jwk, dict) and jwk.get("crv") == "X25519":
                keys.append((kid, unb64(str(jwk["x"]))))
            elif isinstance(method.get("publicKeyMultibase"), str):
                keys.append((kid, x25519_of(method["publicKeyMultibase"])))
        except (DidCommError, KeyError, ValueError):
            continue
    endpoint = None
    for service in document.get("service", []):
        if not isinstance(service, dict) or service.get("type") != "DIDCommMessaging":
            continue
        endpoints = service.get("serviceEndpoint")
        for item in endpoints if isinstance(endpoints, list) else [endpoints]:
            uri = item.get("uri") if isinstance(item, dict) else item
            if isinstance(uri, str) and uri.startswith(("https://", "http://")):
                endpoint = endpoint or uri
    if not did or not keys or endpoint is None:
        raise DidCommError("mediator_unresolvable")
    return Mediator(did=did, keys=keys, endpoint=endpoint)


# JWE --------------------------------------------------------------------------


def _apv(kids: list[str]) -> str:
    return b64(hashlib.sha256(".".join(sorted(kids)).encode()).digest())


def _kdf(z: bytes, alg: str, apu: bytes, apv: bytes, tag: bytes | None) -> bytes:
    """Concat KDF (RFC 7518 §4.6.2) for the A256KW key; ECDH-1PU appends the
    content tag to `SuppPubInfo`."""

    def sized(data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + data

    info = sized(alg.encode()) + sized(apu) + sized(apv) + (256).to_bytes(4, "big")
    if tag is not None:
        info += sized(tag)
    return hashlib.sha256((1).to_bytes(4, "big") + z + info).digest()


def _cbc_hs512(cek: bytes, iv: bytes, aad: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    mac_key, enc_key = cek[:32], cek[32:]
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    al = (len(aad) * 8).to_bytes(8, "big")
    tag = hmac.new(mac_key, aad + iv + ciphertext + al, hashlib.sha512).digest()[:32]
    return ciphertext, tag


def _raw(key: X25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def encrypt(
    plaintext: bytes,
    recipients: list[tuple[str, bytes]],
    sender: tuple[str, X25519PrivateKey] | None = None,
) -> dict[str, Any]:
    """A JWE (general JSON) to `recipients` (kid, X25519 public key):
    authcrypt from `sender` (kid, private key), else anoncrypt."""
    alg = "ECDH-1PU+A256KW" if sender else "ECDH-ES+A256KW"
    ephemeral = X25519PrivateKey.generate()
    kids = [kid for kid, _ in recipients]
    header: dict[str, Any] = {
        "typ": ENCRYPTED,
        "alg": alg,
        "enc": "A256CBC-HS512",
        "epk": {"kty": "OKP", "crv": "X25519", "x": b64(_raw(ephemeral.public_key()))},
    }
    if sender:
        header["skid"] = sender[0]
        header["apu"] = b64(sender[0].encode())
    header["apv"] = _apv(kids)
    protected = b64(json.dumps(header, separators=(",", ":")).encode())
    cek, iv = os.urandom(64), os.urandom(16)
    ciphertext, tag = _cbc_hs512(cek, iv, protected.encode(), plaintext)
    apu = unb64(header["apu"]) if sender else b""
    apv = unb64(header["apv"])
    wrapped = []
    for kid, raw in recipients:
        public = X25519PublicKey.from_public_bytes(raw)
        z = ephemeral.exchange(public)
        if sender:
            z += sender[1].exchange(public)
        kek = _kdf(z, alg, apu, apv, tag if sender else None)
        wrapped.append({"header": {"kid": kid}, "encrypted_key": b64(aes_key_wrap(kek, cek))})
    return {
        "protected": protected,
        "recipients": wrapped,
        "iv": b64(iv),
        "ciphertext": b64(ciphertext),
        "tag": b64(tag),
    }


def decrypt(
    jwe: dict[str, Any], kid: str, key: X25519PrivateKey, sender: bytes | None = None
) -> bytes:
    """The plaintext of `jwe` for recipient `kid`; `sender` is the sender's
    X25519 public key, for authcrypt. For tests and for what comes next."""
    header = json.loads(unb64(jwe["protected"]))
    kids = [r["header"]["kid"] for r in jwe["recipients"]]
    if header["apv"] != _apv(kids):
        raise DidCommError("apv_mismatch")
    wrapped = unb64(
        next(r["encrypted_key"] for r in jwe["recipients"] if r["header"]["kid"] == kid)
    )
    epk = X25519PublicKey.from_public_bytes(unb64(header["epk"]["x"]))
    z = key.exchange(epk)
    tag = unb64(jwe["tag"])
    authcrypt = header["alg"] == "ECDH-1PU+A256KW"
    if authcrypt:
        if sender is None:
            raise DidCommError("sender_required")
        z += key.exchange(X25519PublicKey.from_public_bytes(sender))
    apu = unb64(header["apu"]) if "apu" in header else b""
    kek = _kdf(z, header["alg"], apu, unb64(header["apv"]), tag if authcrypt else None)
    cek = aes_key_unwrap(kek, wrapped)
    iv, ciphertext = unb64(jwe["iv"]), unb64(jwe["ciphertext"])
    al = (len(jwe["protected"]) * 8).to_bytes(8, "big")
    expected = hmac.new(
        cek[:32], jwe["protected"].encode() + iv + ciphertext + al, hashlib.sha512
    ).digest()[:32]
    if not hmac.compare_digest(expected, tag):
        raise DidCommError("tag_mismatch")
    decryptor = Cipher(algorithms.AES(cek[32:]), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# Messages ---------------------------------------------------------------------


def message(
    type_: str, body: dict[str, Any], *, sender: str, to: str, **headers: Any
) -> dict[str, Any]:
    """A plaintext message from `sender` to `to`, made now."""
    return {
        "id": str(uuid.uuid4()),
        "type": type_,
        "typ": PLAIN,
        "from": sender,
        "to": [to],
        "created_time": int(time.time()),
        "body": body,
        **headers,
    }


def pack(
    plain: dict[str, Any],
    *,
    sender_kid: str,
    sender_key: X25519PrivateKey,
    holder: Peer,
    via: Mediator,
) -> str:
    """`plain` authcrypted to `holder`, in a `forward` anoncrypted to `via`."""
    inner = encrypt(
        json.dumps(plain, separators=(",", ":")).encode(),
        [(holder.kid, holder.key)],
        (sender_kid, sender_key),
    )
    forward = {
        "id": str(uuid.uuid4()),
        "type": FORWARD,
        "typ": PLAIN,
        "to": [via.did],
        "created_time": int(time.time()),
        "body": {"next": holder.did},
        "attachments": [{"media_type": ENCRYPTED, "data": {"json": inner}}],
    }
    outer = encrypt(json.dumps(forward, separators=(",", ":")).encode(), via.keys)
    return json.dumps(outer, separators=(",", ":"))


# Transport --------------------------------------------------------------------

# Fetches a DID document (a URL → its JSON) and posts an envelope (a URL and
# the envelope → the HTTP status); tests swap them for fakes.
Fetch = Callable[[str], Awaitable[dict[str, Any]]]
Post = Callable[[str, str], Awaitable[int]]


@dataclass(frozen=True)
class Courier:
    fetch: Fetch
    post: Post

    async def deliver(
        self, plain: dict[str, Any], *, sender_kid: str, sender_key: X25519PrivateKey, to: str
    ) -> None:
        """Packs `plain` for the holder at `to` and hands it to its mediator."""
        holder = peer(to)
        if not holder.service.startswith("did:web:"):
            raise DidCommError("mediator_unresolvable")
        try:
            via = mediator(await self.fetch(did_web_url(holder.service)))
        except (httpx.HTTPError, ValueError):
            raise DidCommError("mediator_unresolvable") from None
        if via.did != holder.service:
            raise DidCommError("mediator_unresolvable")
        envelope = pack(plain, sender_kid=sender_kid, sender_key=sender_key, holder=holder, via=via)
        try:
            status = await self.post(via.endpoint, envelope)
        except httpx.HTTPError:
            raise DidCommError("mediator_unreachable") from None
        if status != 202 and status != 200:
            raise DidCommError(f"mediator_refused_{status}")


async def _fetch(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            url, headers={"accept": "application/did+json, application/json"}
        )
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document


async def _post(url: str, envelope: str) -> int:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.post(url, content=envelope, headers={"content-type": ENCRYPTED})
        return response.status_code


def get_courier() -> Courier:
    return Courier(fetch=_fetch, post=_post)
