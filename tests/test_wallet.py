from typing import Any

import pytest
from httpx import AsyncClient

from registry_api.config import Settings, get_settings
from registry_api.wallet import did_key, public_key
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet, request_uri
from tests.test_account import _sign_in

Headers = dict[str, str]


@pytest.fixture(autouse=True)
def urls(monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = get_settings()
    monkeypatch.setattr(s, "portal_url", "http://portal.test")
    monkeypatch.setattr(s, "public_url", "http://test")
    return s


async def _ask(
    client: AsyncClient, purpose: str = "sign_in", headers: Headers | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/wallet/requests", json={"purpose": purpose}, headers=headers or {}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _answer(client: AsyncClient, asked: dict[str, Any], wallet: FakeWallet) -> int:
    uri = request_uri(asked["deep_link"])
    assert uri == asked["request_uri"]
    request = (await client.get(uri)).json()
    assert request["client_id"] == "http://portal.test"
    answer = await client.post(request["response_uri"], data={"id_token": wallet.id_token(request)})
    return answer.status_code


async def _result(
    client: AsyncClient, asked: dict[str, Any], headers: Headers | None = None
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/auth/wallet/requests/{asked['id']}/result",
        json={"poll": asked["poll"]},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_did_key_round_trips() -> None:
    wallet = FakeWallet()
    assert wallet.did.startswith("did:key:z6Mk")
    raw = public_key(wallet.did).public_bytes_raw()
    assert did_key(raw) == wallet.did


async def test_a_wallet_signs_up_then_in(client: AsyncClient) -> None:
    wallet = FakeWallet()
    asked = await _ask(client)
    assert (await _result(client, asked))["status"] == "pending"
    assert await _answer(client, asked, wallet) == 204

    first = await _result(client, asked)
    assert first["status"] == "signed_in"
    headers = {"Authorization": f"Bearer {first['session']['token']}"}
    assert first["session"]["user"]["email"] is None
    tenants = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert [(t["name"], t["role"]) for t in tenants] == [(None, "admin")]
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=headers)).json()
    assert [(a["provider"], a["did"]) for a in ways["accounts"]] == [("almena", wallet.did)]

    # The answer is good once; the same wallet comes back to the same account.
    gone = await client.post(
        f"/api/v1/auth/wallet/requests/{asked['id']}/result", json={"poll": asked["poll"]}
    )
    assert gone.status_code == 404
    again = await _ask(client)
    await _answer(client, again, wallet)
    second = await _result(client, again)
    assert second["session"]["user"]["id"] == first["session"]["user"]["id"]


async def test_only_the_poll_secret_reads_the_answer(client: AsyncClient) -> None:
    asked = await _ask(client)
    await _answer(client, asked, FakeWallet())
    response = await client.post(
        f"/api/v1/auth/wallet/requests/{asked['id']}/result", json={"poll": "guess"}
    )
    assert response.status_code == 403


async def test_a_request_is_answered_once(client: AsyncClient) -> None:
    asked = await _ask(client)
    assert await _answer(client, asked, FakeWallet()) == 204
    assert await _answer(client, asked, FakeWallet()) == 409


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "http://evil.test"},
        {"nonce": "another"},
        {"sub": "did:key:z6MkotherotherotherotherotherotherotherotherotherX"},
        {"exp": 1},
    ],
)
async def test_a_token_that_does_not_hold_is_refused(
    client: AsyncClient, claims: dict[str, Any]
) -> None:
    asked = await _ask(client)
    request = (await client.get(asked["request_uri"])).json()
    token = FakeWallet().id_token(request, **claims)
    answer = await client.post(request["response_uri"], data={"id_token": token})
    assert answer.status_code == 400
    assert (await _result(client, asked))["status"] == "pending"


async def test_a_token_signed_by_another_key_is_refused(client: AsyncClient) -> None:
    asked = await _ask(client)
    request = (await client.get(asked["request_uri"])).json()
    honest, thief = FakeWallet(), FakeWallet()
    token = thief.id_token(request, iss=honest.did, sub=honest.did)
    answer = await client.post(request["response_uri"], json={"id_token": token})
    assert answer.status_code == 400


async def test_linking_needs_the_account(client: AsyncClient, outbox: Outbox) -> None:
    response = await client.post("/api/v1/auth/wallet/requests", json={"purpose": "link"})
    assert response.status_code == 401

    ada = await _sign_in(client, outbox, "ada@example.org")
    asked = await _ask(client, "link", ada)
    await _answer(client, asked, wallet := FakeWallet())
    # Without Ada's session the answer is not given.
    stranger = await client.post(
        f"/api/v1/auth/wallet/requests/{asked['id']}/result", json={"poll": asked["poll"]}
    )
    assert stranger.status_code == 403

    assert (await _result(client, asked, ada))["status"] == "linked"
    ways = (await client.get("/api/v1/auth/me/ways-in", headers=ada)).json()
    assert [a["did"] for a in ways["accounts"]] == [wallet.did]


async def test_a_wallet_of_another_account_offers_the_move(
    client: AsyncClient, outbox: Outbox
) -> None:
    wallet = FakeWallet()
    asked = await _ask(client)
    await _answer(client, asked, wallet)
    owner = (await _result(client, asked))["session"]["user"]["id"]

    ada = await _sign_in(client, outbox, "ada@example.org")
    link = await _ask(client, "link", ada)
    await _answer(client, link, wallet)
    result = await _result(client, link, ada)
    assert result["status"] == "taken" and result["move_ticket"]

    moved = await client.post(
        "/api/v1/auth/me/move", json={"ticket": result["move_ticket"]}, headers=ada
    )
    assert moved.json()["user"]["id"] == owner
    assert moved.json()["user"]["email"] == "ada@example.org"
