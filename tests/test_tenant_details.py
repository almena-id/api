from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from httpx import AsyncClient

from registry_api.api.routes.auth import get_http_client
from registry_api.main import app
from tests.conftest import Outbox

DID_DOC = {
    "id": "did:web:mediator.example.org",
    "service": [{"type": "DIDCommMessaging", "serviceEndpoint": {"uri": "https://x"}}],
}


def _web(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as c:
            yield c

    app.dependency_overrides[get_http_client] = client
    return seen


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> dict[str, str]:
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _tenant(client: AsyncClient, headers: dict[str, str]) -> str:
    tenant: str = (await client.get("/api/v1/tenants", headers=headers)).json()[0]["id"]
    return tenant


async def test_details_start_without_a_mediator(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    body = (await client.get(f"/api/v1/tenants/{await _tenant(client, ada)}", headers=ada)).json()
    assert body["name"] == "Tenant of ada@example.org"
    assert body["role"] == "admin"
    assert body["mediator_url"] is None and body["mediator_did"] is None


async def test_rename(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    url = f"/api/v1/tenants/{await _tenant(client, ada)}"
    response = await client.patch(url, json={"name": "  Acme  "}, headers=ada)
    assert response.json()["name"] == "Acme"
    blank = await client.patch(url, json={"name": " "}, headers=ada)
    assert blank.status_code == 422 and blank.json()["detail"] == "name_required"


async def test_a_mediator_is_checked_and_its_did_kept(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    url = f"/api/v1/tenants/{await _tenant(client, ada)}"
    seen = _web(lambda _: httpx.Response(200, json=DID_DOC))
    response = await client.patch(
        url, json={"mediator_url": "mediator.example.org/some/path"}, headers=ada
    )
    assert response.status_code == 200, response.text
    assert str(seen[0].url) == "https://mediator.example.org/.well-known/did.json"
    assert response.json()["mediator_url"] == "https://mediator.example.org"
    assert response.json()["mediator_did"] == "did:web:mediator.example.org"

    # The name is left alone when not sent; an empty mediator removes it.
    cleared = await client.patch(url, json={"mediator_url": ""}, headers=ada)
    assert cleared.json()["mediator_url"] is None
    assert cleared.json()["name"] == "Tenant of ada@example.org"


@pytest.mark.parametrize(
    ("typed", "answer", "code"),
    [
        ("http://mediator.example.org", None, "mediator_insecure"),
        ("ftp://mediator.example.org", None, "mediator_invalid"),
        ("https://down.example.org", httpx.Response(503), "mediator_unreachable"),
        ("https://site.example.org", httpx.Response(200, text="<html>"), "mediator_not_a_mediator"),
        ("https://site.example.org", httpx.Response(404), "mediator_not_a_mediator"),
        (
            "https://site.example.org",
            httpx.Response(200, json={"id": "did:key:z6"}),
            "mediator_not_a_mediator",
        ),
        (
            "https://site.example.org",
            httpx.Response(200, json={"id": "did:web:site.example.org", "service": []}),
            "mediator_not_a_mediator",
        ),
    ],
)
async def test_what_is_not_a_mediator_is_refused(
    client: AsyncClient, outbox: Outbox, typed: str, answer: httpx.Response | None, code: str
) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    _web(lambda _: answer or httpx.Response(500))
    response = await client.patch(
        f"/api/v1/tenants/{await _tenant(client, ada)}", json={"mediator_url": typed}, headers=ada
    )
    assert response.status_code == 422
    assert response.json()["detail"] == code


async def test_plain_http_is_fine_on_loopback(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    _web(lambda _: httpx.Response(200, json={**DID_DOC, "id": "did:web:localhost%3A8080"}))
    response = await client.patch(
        f"/api/v1/tenants/{await _tenant(client, ada)}",
        json={"mediator_url": "http://localhost:8080"},
        headers=ada,
    )
    assert response.json()["mediator_url"] == "http://localhost:8080"


async def test_only_admins_change_it(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenant = await _tenant(client, ada)
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "member"},
        headers=ada,
    )
    bob = await _sign_in(client, outbox, "bob@example.org")
    seen = (await client.get(f"/api/v1/tenants/{tenant}", headers=bob)).json()
    assert seen["role"] == "member"
    response = await client.patch(f"/api/v1/tenants/{tenant}", json={"name": "Mine"}, headers=bob)
    assert response.status_code == 403

    eve = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(f"/api/v1/tenants/{tenant}", headers=eve)).status_code == 404
