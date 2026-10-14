"""The `did:peer:2` a holder's wallet names when it pairs with an issuer
(`routes.application_wallet`): the DID it would receive messages at, its
`DIDCommMessaging` service its mediator's `did:web`. The registry checks it
reads as one and keeps it (`Application.holder_messaging_did`).

Nothing is sent to it: writing to a holder needs the issuer's messaging key,
and the platform keeps no private keys today.
"""

import base64
import json
from dataclasses import dataclass
from typing import Any

from registry_api.wallet import WalletError, b58decode

# Multicodec prefix x25519-pub, as multikeys start (`z6LS…`).
_X25519 = b"\xec\x01"
# A did:peer:2 is a few keys and a service: anything longer is not one.
MAX_DID = 2048


class DidCommError(Exception):
    """What could not be done; `code` says what."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
