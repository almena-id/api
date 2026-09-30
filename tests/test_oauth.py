import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from pydantic import SecretStr

from registry_api.api.routes.auth import get_http_client
from registry_api.config import Settings
from registry_api.main import app
from tests.conftest import Outbox

Handler = Callable[[httpx.Request], httpx.Response]
PERSONAL_MICROSOFT = "9188040d-6c67-4c5b-b112-36a304b66dad"


def _provider_answers(handler: Handler) -> list[httpx.Request]:
    """Route every outgoing request to `handler`; returns what was sent."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def client() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as c:
            yield c

    app.dependency_overrides[get_http_client] = client
    return seen


def _id_token(**claims: Any) -> str:
    return jwt.encode(
        {"exp": int(time.time()) + 300, **claims}, "unused-key-" * 4, algorithm="HS256"
    )


async def _start(client: AsyncClient, provider: str) -> dict[str, str]:
    response = await client.post(f"/api/v1/auth/oauth/{provider}/start")
    assert response.status_code == 200, response.text
    body = response.json()
    query = {k: v[0] for k, v in parse_qs(urlparse(body["authorization_url"]).query).items()}
    assert query["state"] == body["state"]
    assert query["redirect_uri"] == f"http://portal.test/auth/{provider}/callback"
    assert query["code_challenge_method"] == "S256"
    return query


async def _finish(client: AsyncClient, provider: str, state: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/auth/oauth/{provider}/callback", json={"code": "the-code", "state": state}
    )


def _google(nonce: Callable[[], str], **claims: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        form = parse_qs(request.content.decode())
        assert form["code"] == ["the-code"] and form["code_verifier"][0]
        token = _id_token(
            **{
                "iss": "https://accounts.google.com",
                "aud": "google-id",
                "sub": "g-123",
                "email": "Ada@Example.org",
                "email_verified": True,
                "nonce": nonce(),
                **claims,
            }
        )
        return httpx.Response(200, json={"id_token": token, "access_token": "x"})

    return handler


async def test_providers_are_off_until_configured(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/auth/providers")).json()
    assert [p["id"] for p in body["providers"]] == ["google", "microsoft", "apple", "github"]
    assert not any(p["enabled"] for p in body["providers"])
    assert (await client.post("/api/v1/auth/oauth/google/start")).status_code == 409
    assert (await client.post("/api/v1/auth/oauth/myspace/start")).status_code == 404


async def test_google_signs_up_then_in(client: AsyncClient, settings: Settings) -> None:
    query = await _start(client, "google")
    _provider_answers(_google(lambda: query["nonce"]))
    first = await _finish(client, "google", query["state"])
    assert first.status_code == 200, first.text
    assert first.json()["user"]["email"] == "ada@example.org"

    headers = {"Authorization": f"Bearer {first.json()['token']}"}
    assert len((await client.get("/api/v1/tenants", headers=headers)).json()) == 1

    query = await _start(client, "google")
    _provider_answers(_google(lambda: query["nonce"]))
    second = await _finish(client, "google", query["state"])
    assert second.json()["user"]["id"] == first.json()["user"]["id"]


async def test_google_joins_the_account_of_the_same_email(
    client: AsyncClient, settings: Settings, outbox: Outbox
) -> None:
    await client.post("/api/v1/auth/code", json={"email": "ada@example.org"})
    by_code = await client.post(
        "/api/v1/auth/verify", json={"email": "ada@example.org", "code": outbox.last_code()}
    )
    query = await _start(client, "google")
    _provider_answers(_google(lambda: query["nonce"]))
    by_google = await _finish(client, "google", query["state"])
    assert by_google.json()["user"]["id"] == by_code.json()["user"]["id"]


async def test_an_unverified_email_is_refused(client: AsyncClient, settings: Settings) -> None:
    query = await _start(client, "google")
    _provider_answers(_google(lambda: query["nonce"], email_verified=False))
    response = await _finish(client, "google", query["state"])
    assert response.status_code == 403
    assert response.json()["detail"] == "email_unverified"


async def test_a_state_works_once(client: AsyncClient, settings: Settings) -> None:
    query = await _start(client, "google")
    _provider_answers(_google(lambda: query["nonce"]))
    assert (await _finish(client, "google", query["state"])).status_code == 200
    response = await _finish(client, "google", query["state"])
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_state"


async def test_a_foreign_nonce_is_refused(client: AsyncClient, settings: Settings) -> None:
    query = await _start(client, "google")
    _provider_answers(_google(lambda: "somebody-else's"))
    response = await _finish(client, "google", query["state"])
    assert response.status_code == 502


async def test_a_provider_error_is_reported(client: AsyncClient, settings: Settings) -> None:
    query = await _start(client, "google")
    _provider_answers(lambda _: httpx.Response(400, json={"error": "invalid_grant"}))
    response = await _finish(client, "google", query["state"])
    assert response.status_code == 502
    assert response.json()["detail"] == "provider_error"


def _microsoft(nonce: Callable[[], str], tid: str, **claims: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "login.microsoftonline.com"
        token = _id_token(
            iss=f"https://login.microsoftonline.com/{tid}/v2.0",
            aud="ms-id",
            sub="m-1",
            tid=tid,
            email="ada@example.org",
            nonce=nonce(),
            **claims,
        )
        return httpx.Response(200, json={"id_token": token})

    return handler


@pytest.mark.parametrize(
    ("tid", "claims", "status"),
    [
        (PERSONAL_MICROSOFT, {}, 200),
        ("some-company", {}, 403),
        ("some-company", {"xms_edov": True}, 200),
    ],
)
async def test_microsoft_trusts_only_verified_domains(
    client: AsyncClient, settings: Settings, tid: str, claims: dict[str, Any], status: int
) -> None:
    query = await _start(client, "microsoft")
    _provider_answers(_microsoft(lambda: query["nonce"], tid, **claims))
    assert (await _finish(client, "microsoft", query["state"])).status_code == status


def _github(emails: list[dict[str, Any]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(200, json={"access_token": "gh-token"})
        assert request.headers["Authorization"] == "Bearer gh-token"
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 42, "login": "ada"})
        return httpx.Response(200, json=emails)

    return handler


async def test_github_uses_the_primary_verified_email(
    client: AsyncClient, settings: Settings
) -> None:
    query = await _start(client, "github")
    _provider_answers(
        _github(
            [
                {"email": "old@example.org", "primary": False, "verified": True},
                {"email": "Ada@Example.org", "primary": True, "verified": True},
            ]
        )
    )
    response = await _finish(client, "github", query["state"])
    assert response.json()["user"]["email"] == "ada@example.org"


async def test_github_without_a_verified_email_is_refused(
    client: AsyncClient, settings: Settings
) -> None:
    query = await _start(client, "github")
    _provider_answers(_github([{"email": "ada@example.org", "primary": True, "verified": False}]))
    assert (await _finish(client, "github", query["state"])).status_code == 403


async def test_apple_signs_its_client_secret(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    for name, value in {
        "apple_client_id": "id.almena.registry",
        "apple_team_id": "TEAM123",
        "apple_key_id": "KEY123",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "apple_private_key", SecretStr(pem))

    query = await _start(client, "apple")
    assert query["response_mode"] == "form_post"

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        secret = form["client_secret"][0]
        assert jwt.get_unverified_header(secret)["kid"] == "KEY123"
        claims = jwt.decode(
            secret, key.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com"
        )
        assert claims["iss"] == "TEAM123" and claims["sub"] == "id.almena.registry"
        token = _id_token(
            iss="https://appleid.apple.com",
            aud="id.almena.registry",
            sub="a-1",
            email="ada@privaterelay.appleid.com",
            email_verified="true",
            nonce=query["nonce"],
        )
        return httpx.Response(200, json={"id_token": token})

    _provider_answers(handler)
    response = await _finish(client, "apple", query["state"])
    assert response.status_code == 200, response.text
    assert json.loads(response.text)["user"]["email"] == "ada@privaterelay.appleid.com"
