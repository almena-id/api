"""did:webvh 1.0 logs: building entries, and checking the proofs on them.

A did:webvh DID is ``did:webvh:{SCID}:{host}[:path]``; its history is a JSON
Lines log served at ``https://{host}/{path}/did.jsonl`` (``/.well-known/`` for
a bare host). Each entry is ``{versionId, versionTime, parameters, state,
proof}``: ``state`` the DID document at that version, ``parameters`` what
changed in the method's settings (the first one names the method, the SCID and
the ``updateKeys``), and ``proof`` a Data Integrity proof (``eddsa-jcs-2022``)
by one of the update keys in force before it.

The registry builds entries and never signs them: people sign them in their
wallets (``api/routes/wallet.py``) and the registry checks the proof
before it writes the entry to the log.

Hashes are multihash SHA-256 in base58btc (``Qm…``), over the JSON
Canonicalization Scheme (RFC 8785) of the entry.
"""

import copy
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from cryptography.exceptions import InvalidSignature

from registry_api import wallet

METHOD = "did:webvh:1.0"
PLACEHOLDER = "{SCID}"
CRYPTOSUITE = "eddsa-jcs-2022"

Entry = dict[str, Any]


class LogError(Exception):
    """An entry or a proof that does not hold; `code` says which."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def jcs(value: Any) -> bytes:
    """RFC 8785 for what DID documents hold: objects, arrays, strings, integers,
    booleans and null (no floats: none are written here)."""

    def write(item: Any) -> str:
        if isinstance(item, dict):
            # Keys sort by their UTF-16 code units.
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return (
                "{"
                + ",".join(json.dumps(k, ensure_ascii=False) + ":" + write(item[k]) for k in keys)
                + "}"
            )
        if isinstance(item, list):
            return "[" + ",".join(write(element) for element in item) + "]"
        if isinstance(item, bool) or item is None or isinstance(item, (str, int)):
            return json.dumps(item, ensure_ascii=False)
        raise LogError("not_canonical")

    return write(value).encode()


def multihash(data: bytes) -> str:
    """SHA-256 as a base58btc multihash: what SCIDs and entry hashes are."""
    return wallet.b58encode(b"\x12\x20" + hashlib.sha256(data).digest())


def did_prefix(host: str, path: Iterable[str] = ()) -> str:
    """`did:webvh:{SCID}:{host}[:path]` with the SCID still a placeholder."""
    return ":".join(["did:webvh", PLACEHOLDER, host, *path])


def multikey(did_key: str) -> str:
    """The update key form of a `did:key`: its multibase body."""
    return did_key.removeprefix("did:key:")


def _entry_hash(entry: Entry) -> str:
    unsigned = {k: v for k, v in entry.items() if k != "proof"}
    return multihash(jcs(unsigned))


def genesis(did_template: str, state: dict[str, Any], update_keys: list[str], when: str) -> Entry:
    """The first entry: the SCID is the hash of the entry written with a
    placeholder where the SCID goes, and then put in its place."""
    preliminary: Entry = {
        "versionId": PLACEHOLDER,
        "versionTime": when,
        "parameters": {"method": METHOD, "scid": PLACEHOLDER, "updateKeys": update_keys},
        "state": {**state, "id": did_template},
    }
    scid = multihash(jcs(preliminary))
    entry: Entry = json.loads(json.dumps(preliminary).replace(PLACEHOLDER, scid))
    entry["versionId"] = f"1-{_entry_hash(entry)}"
    return entry


def following(
    previous: Entry, parameters: dict[str, Any], state: dict[str, Any], when: str
) -> Entry:
    """The entry after `previous`: its hash is taken with the previous
    versionId in place of its own."""
    number = int(str(previous["versionId"]).split("-", 1)[0]) + 1
    entry: Entry = {
        "versionId": previous["versionId"],
        "versionTime": when,
        "parameters": parameters,
        "state": state,
    }
    entry["versionId"] = f"{number}-{_entry_hash(entry)}"
    return entry


def scid_of(entries: list[Entry]) -> str:
    return str(entries[0]["parameters"]["scid"])


def active_update_keys(entries: list[Entry]) -> list[str]:
    """The update keys in force after the last entry."""
    keys: list[str] = []
    for entry in entries:
        if "updateKeys" in entry.get("parameters", {}):
            keys = list(entry["parameters"]["updateKeys"])
    return keys


def signers_for(entries: list[Entry], entry: Entry) -> list[str]:
    """Who may sign `entry`: the keys in force before it — for the first one,
    the keys it names itself."""
    if not entries:
        return list(entry["parameters"]["updateKeys"])
    return active_update_keys(entries)


def _proof_hash(entry: Entry, proof: dict[str, Any]) -> bytes:
    options = {k: v for k, v in proof.items() if k != "proofValue"}
    unsigned = {k: v for k, v in entry.items() if k != "proof"}
    return hashlib.sha256(jcs(options)).digest() + hashlib.sha256(jcs(unsigned)).digest()


def check_proof(
    document: dict[str, Any],
    proof: Any,
    allowed: list[str],
    *,
    controller: str | None = None,
    purpose: str = "assertionMethod",
    refusal: str = "not_an_update_key",
) -> str:
    """The `did:key` of the key whose `eddsa-jcs-2022` signature `proof` is
    over `document`, if that key is one of the `allowed` multikeys; otherwise
    `LogError` (`refusal` when the key is not allowed).

    Log entries are signed by a key named as a `did:key` (`did:key:z…#z…`);
    with `controller`, the key is named as one of that DID's verification
    methods (`{controller}#z…`), as a credential's or a presentation's is.
    """
    if not isinstance(proof, dict):
        raise LogError("invalid_proof")
    method = proof.get("verificationMethod")
    value = proof.get("proofValue")
    if (
        proof.get("type") != "DataIntegrityProof"
        or proof.get("cryptosuite") != CRYPTOSUITE
        or proof.get("proofPurpose") != purpose
        or not isinstance(method, str)
        or not isinstance(value, str)
        or not value.startswith("z")
        # A document with a context signs it as part of the proof options.
        or proof.get("@context") != document.get("@context")
    ):
        raise LogError("invalid_proof")
    named, _, key = method.partition("#")
    expected = controller if controller is not None else f"did:key:{key}"
    if named != expected:
        raise LogError("invalid_proof")
    if key not in allowed:
        raise LogError(refusal)
    did = f"did:key:{key}"
    try:
        public = wallet.public_key(did)
        signature = wallet.b58decode(value[1:])
        public.verify(signature, _proof_hash(document, proof))
    except (wallet.WalletError, InvalidSignature, ValueError):
        raise LogError("invalid_proof") from None
    return did


def signed(entry: Entry, proof: dict[str, Any]) -> Entry:
    return {**copy.deepcopy(entry), "proof": [proof]}


def as_did_web(document: dict[str, Any], scid: str) -> dict[str, Any]:
    """The same document under its did:web name, for resolvers that know only
    did:web: `did:webvh:{SCID}:` becomes `did:web:`, and the did:webvh DID is
    named in `alsoKnownAs`."""
    webvh_did = str(document["id"])
    text = json.dumps(document).replace(f"did:webvh:{scid}:", "did:web:")
    web: dict[str, Any] = json.loads(text)
    known = [aka for aka in web.get("alsoKnownAs", []) if aka != webvh_did]
    web["alsoKnownAs"] = [*known, webvh_did]
    return web
