import base64
import hashlib
import json
import secrets
import time
import zlib
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient

from registry_api.main import app
from registry_api.presentations import get_status_fetch
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.signing import publish, ready, sign
from tests.test_directory import _sign_in

AUD = "https://registry.almena.id"
NONCE = "n-0S6_WzA2Mj"
VCT = "https://almena.id/credentials/membership/v1"
CLAIMS = {
    "given_name": "Lucía",
    "family_name": "García",
    "organization_name": "Club",
    "member_number": "0042",
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Issuer:
    """A published registry issuer: its DID and the wallet whose key it lists."""

    def __init__(self, did: str, wallet: FakeWallet) -> None:
        self.did, self.wallet = did, wallet

    def sign(self, payload: dict[str, Any], typ: str) -> str:
        key = self.wallet.did.removeprefix("did:key:")
        return jwt.encode(
            payload,
            self.wallet.key,
            algorithm="EdDSA",
            headers={"typ": typ, "kid": f"{self.did}#{key}"},
        )


def holder_jwk(holder: FakeWallet) -> dict[str, str]:
    raw = holder.key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {"kty": "OKP", "crv": "Ed25519", "x": b64(raw)}


def sd_jwt(
    issuer: Issuer,
    holder: FakeWallet,
    claims: dict[str, Any] = CLAIMS,
    *,
    vct: str = VCT,
    nonce: str = NONCE,
    aud: str = AUD,
    lifetime: int = 3600,
    status: dict[str, Any] | None = None,
    extra_disclosure: bool = False,
    binding: bool = True,
) -> str:
    disclosures = [
        b64(json.dumps([secrets.token_hex(8), name, value]).encode())
        for name, value in claims.items()
    ]
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer.did,
        "iat": now,
        "exp": now + lifetime,
        "vct": vct,
        "_sd_alg": "sha-256",
        "_sd": sorted(b64(hashlib.sha256(d.encode()).digest()) for d in disclosures),
        "cnf": {"jwk": holder_jwk(holder)},
    }
    if status:
        payload["status"] = status
    if extra_disclosure:
        disclosures.append(b64(json.dumps(["salt", "role", "admin"]).encode()))
    issued = issuer.sign(payload, "dc+sd-jwt")
    presented = issued + "~" + "".join(d + "~" for d in disclosures)
    if not binding:
        return presented
    kb = jwt.encode(
        {
            "iat": now,
            "aud": aud,
            "nonce": nonce,
            "sd_hash": b64(hashlib.sha256(presented.encode()).digest()),
        },
        holder.key,
        algorithm="EdDSA",
        headers={"typ": "kb+jwt"},
    )
    return presented + kb


def w3c(
    issuer: Issuer, holder: FakeWallet, *, nonce: str = NONCE, subject: str | None = None
) -> str:
    now = int(time.time())
    credential = issuer.sign(
        {
            "iss": issuer.did,
            "sub": holder.did,
            "nbf": now - 10,
            "exp": now + 3600,
            "vc": {
                "type": ["VerifiableCredential", "MembershipCredential"],
                "credentialSubject": {"id": subject or holder.did, **CLAIMS},
            },
        },
        "JWT",
    )
    return jwt.encode(
        {
            "iss": holder.did,
            "aud": AUD,
            "nonce": nonce,
            "iat": now,
            "vp": {"type": ["VerifiablePresentation"], "verifiableCredential": [credential]},
        },
        holder.key,
        algorithm="EdDSA",
    )


async def _issuer(client: AsyncClient, outbox: Outbox, email: str, types: list[str]) -> Issuer:
    headers, tenant = await _sign_in(client, outbox, email)
    base = f"/api/v1/tenants/{tenant}/issuers"
    created = (await client.post(base, json={"name": "Club"}, headers=headers)).json()
    await client.put(
        f"{base}/{created['id']}/credential-types", json={"types": types}, headers=headers
    )
    signer = await ready(client, headers, tenant)
    # Its credentials are signed by its signer's wallet: the key its DID lists.
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    await client.put(
        f"{base}/{created['id']}/signing",
        json={"system": "single_user", "user_id": me["id"]},
        headers=headers,
    )
    await sign(client, headers, tenant, created["identity"]["id"], signer)
    await publish(client, headers, tenant, "issuers", created["id"], signer)
    detail = (await client.get(f"{base}/{created['id']}", headers=headers)).json()
    return Issuer(detail["did"], signer)


async def _form(
    client: AsyncClient, outbox: Outbox, credentials: list[dict[str, Any]]
) -> tuple[dict[str, str], str]:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    response = await client.post(
        f"/api/v1/tenants/{tenant}/forms",
        json={
            "name": {"en": "Members' discount"},
            "fields": [{"ref": "given_name"}, {"ref": "family_name"}, {"ref": "email"}],
            "credentials": credentials,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return headers, f"/api/v1/tenants/{tenant}/forms/{response.json()['id']}/verify"


async def _verify(
    client: AsyncClient,
    headers: dict[str, str],
    url: str,
    vp_token: dict[str, list[str]],
    nonce: str = NONCE,
) -> dict[str, Any]:
    response = await client.post(
        url, json={"vp_token": vp_token, "nonce": nonce, "audience": AUD}, headers=headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_presented_sd_jwt_vc_is_verified_and_fills_the_form(
    client: AsyncClient, outbox: Outbox
) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    holder = FakeWallet()
    headers, url = await _form(
        client,
        outbox,
        [{"type": "membership", "claims": ["given_name", "family_name", "member_number"]}],
    )
    result = await _verify(client, headers, url, {"membership_sd_jwt": [sd_jwt(club, holder)]})
    assert result["verified"] is True
    entry = result["credentials"][0]
    assert entry["format"] == "dc+sd-jwt" and entry["issuer"] == club.did
    # Only what the form asks for, though more was disclosed.
    assert entry["claims"] == {
        "given_name": "Lucía",
        "family_name": "García",
        "member_number": "0042",
    }
    assert entry["fills"] == {"given_name": "Lucía", "family_name": "García"}
    assert entry["problems"] == []

    # The issuer by its did:web alias resolves the same.
    slug = club.did.rsplit(":", 1)[1]
    alias = Issuer(f"did:web:almena.id:ids:{slug}", club.wallet)
    again = await _verify(client, headers, url, {"membership_sd_jwt": [sd_jwt(alias, holder)]})
    assert again["verified"] is True


async def test_a_presented_w3c_credential_is_verified(client: AsyncClient, outbox: Outbox) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    holder = FakeWallet()
    headers, url = await _form(client, outbox, [{"type": "membership"}])
    result = await _verify(client, headers, url, {"membership_w3c": [w3c(club, holder)]})
    assert result["verified"] is True
    assert result["credentials"][0]["format"] == "jwt_vc_json"
    assert result["credentials"][0]["claims"]["member_number"] == "0042"

    for presentation, problem in (
        (w3c(club, holder, nonce="other"), "nonce_mismatch"),
        (w3c(club, holder, subject=FakeWallet().did), "holder_binding_invalid"),
    ):
        failed = await _verify(client, headers, url, {"membership_w3c": [presentation]})
        assert failed["verified"] is False
        assert failed["credentials"][0]["problems"] == [problem]


async def test_nothing_is_believed_on_a_partial_check(client: AsyncClient, outbox: Outbox) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    gym = await _issuer(client, outbox, "gym@example.org", ["employment"])
    holder = FakeWallet()
    headers, url = await _form(client, outbox, [{"type": "membership"}])
    forged = Issuer(club.did, FakeWallet())
    stranger = FakeWallet()
    partial = {k: v for k, v in CLAIMS.items() if k != "member_number"}

    for presentation, problem in (
        (sd_jwt(club, holder, nonce="replayed"), "nonce_mismatch"),
        (sd_jwt(club, holder, aud="https://elsewhere.example"), "audience_mismatch"),
        (sd_jwt(club, holder, binding=False), "holder_binding_invalid"),
        (sd_jwt(club, holder, extra_disclosure=True), "disclosure_invalid"),
        (sd_jwt(club, holder, partial), "claims_missing"),
        (sd_jwt(club, holder, lifetime=-3600), "expired"),
        (sd_jwt(club, holder, vct="https://almena.id/credentials/employment/v1"), "type_mismatch"),
        (sd_jwt(forged, holder), "signature_invalid"),
        # An issuer that does not grant the type, and one outside the registry.
        (sd_jwt(Issuer(gym.did, gym.wallet), holder), "issuer_untrusted"),
        (sd_jwt(Issuer(stranger.did, stranger), holder), "issuer_untrusted"),
    ):
        result = await _verify(client, headers, url, {"membership_sd_jwt": [presentation]})
        assert result["verified"] is False, problem
        assert result["credentials"][0]["problems"] == [problem]
        assert result["credentials"][0]["claims"] == {} and result["credentials"][0]["fills"] == {}

    # A credential bound to another key cannot be presented by whoever holds it.
    taken = sd_jwt(club, holder)
    stolen = taken[: taken.rindex("~") + 1] + jwt.encode(
        {"iat": int(time.time()), "aud": AUD, "nonce": NONCE, "sd_hash": "x"},
        stranger.key,
        algorithm="EdDSA",
        headers={"typ": "kb+jwt"},
    )
    result = await _verify(client, headers, url, {"membership_sd_jwt": [stolen]})
    assert result["credentials"][0]["problems"] == ["holder_binding_invalid"]


async def test_trust_is_the_forms(client: AsyncClient, outbox: Outbox) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    other = await _issuer(client, outbox, "other@example.org", ["membership"])
    holder = FakeWallet()
    headers, url = await _form(
        client,
        outbox,
        [
            {"type": "membership", "trust": "issuers", "issuers": [club.did]},
            {"key": "identity", "type": "pid", "required": False},
        ],
    )
    trusted = await _verify(client, headers, url, {"membership_sd_jwt": [sd_jwt(club, holder)]})
    # The optional PID left out: still verified.
    assert trusted["verified"] is True
    assert trusted["credentials"][1] == {
        "key": "identity",
        "presented": False,
        "verified": False,
        "query": None,
        "format": None,
        "issuer": None,
        "claims": {},
        "fills": {},
        "problems": [],
    }
    untrusted = await _verify(client, headers, url, {"membership_sd_jwt": [sd_jwt(other, holder)]})
    assert untrusted["credentials"][0]["problems"] == ["issuer_untrusted"]
    # The required one missing.
    missing = await _verify(client, headers, url, {})
    assert missing["verified"] is False
    assert missing["credentials"][0]["problems"] == ["not_presented"]
    # The EU PID is reported, not believed: its framework is not checked yet.
    pid = sd_jwt(Issuer(FakeWallet().did, FakeWallet()), holder, vct="urn:eudi:pid:1")
    framework = await _verify(
        client,
        headers,
        url,
        {"membership_sd_jwt": [sd_jwt(club, holder)], "identity_sd_jwt": [pid]},
    )
    assert framework["verified"] is False
    assert framework["credentials"][1]["problems"] == ["trust_framework_unsupported"]


async def test_a_revoked_credential_is_refused(client: AsyncClient, outbox: Outbox) -> None:
    club = await _issuer(client, outbox, "club@example.org", ["membership"])
    holder = FakeWallet()
    headers, url = await _form(client, outbox, [{"type": "membership"}])
    uri = "https://club.example/status/1"
    # Index 3 revoked (1 bit per entry), the rest valid.
    lst = b64(zlib.compress(bytes([0b00001000, 0])))
    lists = {
        uri: club.sign(
            {"sub": uri, "iat": int(time.time()), "status_list": {"bits": 1, "lst": lst}},
            "statuslist+jwt",
        )
    }

    async def fetch(url: str) -> str:
        if url not in lists:
            raise OSError("unreachable")
        return lists[url]

    app.dependency_overrides[get_status_fetch] = lambda: fetch
    for index, uri_used, expected in (
        (2, uri, []),
        (3, uri, ["revoked"]),
        (2, "https://club.example/status/gone", ["status_unverifiable"]),
    ):
        status = {"status_list": {"idx": index, "uri": uri_used}}
        result = await _verify(
            client, headers, url, {"membership_sd_jwt": [sd_jwt(club, holder, status=status)]}
        )
        assert result["credentials"][0]["problems"] == expected, index
