"""A stand-in for the Almena wallet: answers a request the way the wallet will.

In tests it is a class; in development, ``task wallet -- '<deep link>'``
answers a request shown by the portal, with a key kept in ``.fake-wallet-key``
(the same DID every time, so it signs in to the same account).
"""

import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from registry_api import webvh
from registry_api.wallet import b58encode, did_key


class FakeWallet:
    def __init__(self, key: Ed25519PrivateKey | None = None) -> None:
        self.key = key or Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.did = did_key(raw)

    def id_token(self, request: dict[str, Any], **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": self.did,
            "sub": self.did,
            "aud": request["client_id"],
            "nonce": request["nonce"],
            "iat": now,
            "exp": now + 120,
            **claims,
        }
        fragment = self.did.removeprefix("did:key:")
        return jwt.encode(
            payload, self.key, algorithm="EdDSA", headers={"kid": f"{self.did}#{fragment}"}
        )

    def proof(
        self,
        document: dict[str, Any],
        controller: str | None = None,
        purpose: str = "assertionMethod",
    ) -> dict[str, Any]:
        """An `eddsa-jcs-2022` Data Integrity proof over `document`: a log
        entry (the key named as its `did:key`), or with `controller` a
        credential or presentation (the key named as one of that DID's)."""
        key = self.did.removeprefix("did:key:")
        options: dict[str, Any] = {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "verificationMethod": f"{controller or self.did}#{key}",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "proofPurpose": purpose,
        }
        if "@context" in document:
            options["@context"] = document["@context"]
        digest = hashlib.sha256(webvh.jcs(options)).digest()
        digest += hashlib.sha256(webvh.jcs(document)).digest()
        return {**options, "proofValue": "z" + b58encode(self.key.sign(digest))}

    def present(
        self, request: dict[str, Any], credential: str, claims: list[str] | None = None
    ) -> str:
        """An SD-JWT VC it holds, presented for `request`: only the
        disclosures of `claims` (all of them when `None`), and a key-binding
        JWT for the request's `client_id` and `nonce` over what it shows."""
        issued, *disclosures = [part for part in credential.split("~") if part]
        if claims is not None:
            disclosures = [d for d in disclosures if _disclosed_name(d) in claims]
        shown = issued + "~" + "".join(d + "~" for d in disclosures)
        digest = base64.urlsafe_b64encode(hashlib.sha256(shown.encode()).digest())
        binding = jwt.encode(
            {
                "iat": int(time.time()),
                "aud": request["client_id"],
                "nonce": request["nonce"],
                "sd_hash": digest.decode().rstrip("="),
            },
            self.key,
            algorithm="EdDSA",
            headers={"typ": "kb+jwt"},
        )
        return shown + binding

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """What the wallet posts back: an `id_token`, or a `proof` to sign."""
        if request["purpose"] == "sign":
            sign = request["sign"]
            proof = self.proof(sign["document"], sign["verification_method"], sign["proof_purpose"])
            return {"proof": proof}
        return {"id_token": self.id_token(request)}


def _disclosed_name(disclosure: str) -> str:
    decoded = json.loads(base64.urlsafe_b64decode(disclosure + "=" * (-len(disclosure) % 4)))
    return str(decoded[1]) if len(decoded) == 3 else ""


def request_uri(deep_link: str) -> str:
    return parse_qs(urlparse(deep_link).query)["request_uri"][0]


def _key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(loaded, Ed25519PrivateKey)
        return loaded
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(
            "usage: task wallet -- "
            "'<https://almena.id/w/auth?request_uri=…, almena://auth?… or the request URI>'"
        )
    given = sys.argv[1]
    # The link (https or almena://) carries the request's address; or it is the address.
    uri = request_uri(given) if "request_uri=" in given else given
    wallet = FakeWallet(_key(Path(".fake-wallet-key")))
    with httpx.Client(timeout=10) as http:
        request = http.get(uri).raise_for_status().json()
        print(f"{request['client_name']} ({request['client_id']}) asks to {request['purpose']}")
        print(f"answering as {wallet.did}")
        if request["purpose"] == "sign":
            sign = request["sign"]
            print(f"signing the {sign['kind']} of {sign['identity']} ({sign['did']})")
            answer = http.post(request["response_uri"], json=wallet.answer(request))
        else:
            answer = http.post(request["response_uri"], data=wallet.answer(request))
        print(answer.status_code, answer.text or "ok")


if __name__ == "__main__":
    main()
