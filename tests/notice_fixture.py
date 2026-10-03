"""Writes the fixture almena-didcomm checks the registry's notices against:
``uv run python -m tests.notice_fixture > ../mediator/crates/didcomm/tests/registry/notice.json``
(fresh keys each time; the mediator's `registry_notice.rs` unpacks it)."""

import json
import sys
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from registry_api import didcomm
from registry_api.messaging_keys import multikey
from registry_api.wallet import b58encode

MEDIATOR = "did:web:mediator.example.org"
ISSUER = "did:web:almena.id:ids:idn_notice"
APPLICATION = "0b7c1e52-4f3a-4c8e-9d61-2a5f0e9b7c13"


def jwk(key: X25519PrivateKey, *, private: bool = True) -> dict[str, str]:
    found = {"kty": "OKP", "crv": "X25519", "x": didcomm.b64(key.public_key().public_bytes_raw())}
    if private:
        found["d"] = didcomm.b64(key.private_bytes_raw())
    return found


def fixture() -> dict[str, Any]:
    mediator_key = X25519PrivateKey.generate()
    mediator_doc = {
        "id": MEDIATOR,
        "verificationMethod": [
            {
                "id": f"{MEDIATOR}#key-x25519",
                "type": "JsonWebKey2020",
                "controller": MEDIATOR,
                "publicKeyJwk": jwk(mediator_key, private=False),
            }
        ],
        "keyAgreement": [f"{MEDIATOR}#key-x25519"],
        "service": [
            {
                "id": f"{MEDIATOR}#didcomm",
                "type": "DIDCommMessaging",
                "serviceEndpoint": [
                    {"uri": "https://mediator.example.org/didcomm", "accept": ["didcomm/v2"]}
                ],
            }
        ],
    }
    issuer_key = X25519PrivateKey.generate()
    agreement = multikey(issuer_key.public_key().public_bytes_raw())
    issuer_doc = {
        "@context": ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/multikey/v1"],
        "id": ISSUER,
        "verificationMethod": [
            {
                "id": f"{ISSUER}#{agreement}",
                "type": "Multikey",
                "controller": ISSUER,
                "publicKeyMultibase": agreement,
            }
        ],
        "keyAgreement": [f"{ISSUER}#{agreement}"],
    }
    holder_key = X25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    service = {"t": "dm", "s": {"uri": MEDIATOR, "a": ["didcomm/v2"]}}
    holder = (
        "did:peer:2"
        f".Vz{b58encode(b'\xed\x01' + signing)}"
        f".E{multikey(holder_key.public_key().public_bytes_raw())}"
        f".S{didcomm.b64(json.dumps(service, separators=(',', ':')).encode())}"
    )
    body = {
        "application": APPLICATION,
        "status": "issued",
        "credential_type": "membership",
        "credential_name": {"en": "Membership", "es": "Afiliación"},
        "issuer": "Club",
        "collect": f"https://api.almena.id/api/v1/applications/{APPLICATION}/collect",
    }
    plain = didcomm.message(
        "https://almena.id/protocols/application/1.0/status",
        body,
        sender=ISSUER,
        to=holder,
        thid=APPLICATION,
    )
    envelope = didcomm.pack(
        plain,
        sender_kid=f"{ISSUER}#{agreement}",
        sender_key=issuer_key,
        holder=didcomm.peer(holder),
        via=didcomm.mediator(mediator_doc),
    )
    return {
        "about": "A holder notice the registry API (registry_api.didcomm) packed.",
        "mediator": {
            "document": mediator_doc,
            "secret": {"kid": f"{MEDIATOR}#key-x25519", **jwk(mediator_key)},
        },
        "issuer": {"document": issuer_doc},
        "holder": {"did": holder, "secret": {"kid": f"{holder}#key-2", **jwk(holder_key)}},
        "message": plain,
        "envelope": envelope,
    }


if __name__ == "__main__":
    json.dump(fixture(), sys.stdout, indent=2)
    print()
