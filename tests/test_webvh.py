import json

import pytest

from registry_api import webvh
from tests.fake_wallet import FakeWallet

TEMPLATE = "did:webvh:{SCID}:almena.id:ids:idn_x"
WHEN = "2026-09-30T10:00:00Z"


def _genesis(wallet: FakeWallet) -> webvh.Entry:
    state = {"@context": ["https://www.w3.org/ns/did/v1"], "id": TEMPLATE}
    return webvh.genesis(TEMPLATE, state, [webvh.multikey(wallet.did)], WHEN)


def test_jcs_sorts_and_compacts() -> None:
    assert webvh.jcs({"b": [1, True, None], "a": "é"}) == '{"a":"é","b":[1,true,null]}'.encode()
    with pytest.raises(webvh.LogError):
        webvh.jcs({"x": 1.5})


def test_the_scid_is_the_hash_of_the_entry_that_names_it() -> None:
    entry = _genesis(FakeWallet())
    scid = entry["parameters"]["scid"]
    assert scid.startswith("Qm") and entry["state"]["id"] == f"did:webvh:{scid}:almena.id:ids:idn_x"
    # Put the placeholder back where the SCID is: its hash is the SCID.
    preliminary = json.loads(json.dumps(entry).replace(scid, "{SCID}"))
    preliminary["versionId"] = "{SCID}"
    assert webvh.multihash(webvh.jcs(preliminary)) == scid
    # And the version id hashes the entry with the SCID as its version id.
    number, entry_hash = entry["versionId"].split("-")
    assert number == "1"
    assert webvh.multihash(webvh.jcs({**entry, "versionId": scid})) == entry_hash


def test_each_entry_chains_to_the_one_before() -> None:
    first = _genesis(FakeWallet())
    second = webvh.following(first, {}, first["state"], WHEN)
    number, entry_hash = second["versionId"].split("-")
    assert number == "2"
    assert webvh.multihash(webvh.jcs({**second, "versionId": first["versionId"]})) == entry_hash


def test_a_proof_holds_only_for_its_entry_and_its_keys() -> None:
    wallet = FakeWallet()
    entry = _genesis(wallet)
    proof = wallet.proof(entry)
    keys = [webvh.multikey(wallet.did)]
    assert webvh.check_proof(entry, proof, keys) == wallet.did

    tampered = {**entry, "versionTime": "2030-01-01T00:00:00Z"}
    with pytest.raises(webvh.LogError, match="invalid_proof"):
        webvh.check_proof(tampered, proof, keys)
    with pytest.raises(webvh.LogError, match="not_an_update_key"):
        webvh.check_proof(entry, proof, [webvh.multikey(FakeWallet().did)])
    with pytest.raises(webvh.LogError, match="invalid_proof"):
        webvh.check_proof(entry, {**proof, "cryptosuite": "eddsa-rdfc-2022"}, keys)


def test_the_did_web_copy_names_the_webvh_did() -> None:
    entry = _genesis(FakeWallet())
    scid = webvh.scid_of([entry])
    web = webvh.as_did_web(entry["state"], scid)
    assert web["id"] == "did:web:almena.id:ids:idn_x"
    assert web["alsoKnownAs"] == [entry["state"]["id"]]
