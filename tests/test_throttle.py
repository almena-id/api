import time
from datetime import timedelta

import pytest
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from registry_api import patterns, throttle
from registry_api.config import get_settings
from registry_api.models import LoginCode, Throttle
from tests.conftest import Outbox

EMAIL = "ada@example.org"


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No wait between codes, so a test may ask for several in a row."""
    monkeypatch.setattr(get_settings(), "login_code_resend_seconds", 0)
    return monkeypatch


async def _ask(client: AsyncClient, email: str = EMAIL, **headers: str) -> Response:
    return await client.post("/api/v1/auth/code", json={"email": email}, headers=headers)


async def _verify(client: AsyncClient, code: str, email: str = EMAIL) -> Response:
    return await client.post("/api/v1/auth/verify", json={"email": email, "code": code})


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def _age(db: AsyncSession, by: timedelta) -> None:
    """Moves every counter's window, and every code, back by `by`."""
    db.expire_all()
    for row in await db.scalars(select(Throttle)):
        row.window_start -= by
    for code in await db.scalars(select(LoginCode)):
        code.created_at -= by
    await db.commit()


async def test_a_new_code_waits_a_minute(
    client: AsyncClient, outbox: Outbox, db: AsyncSession
) -> None:
    assert (await _ask(client)).status_code == 202
    again = await _ask(client)
    assert again.status_code == 429
    assert again.json()["detail"] == "too_many_codes"
    assert 0 < int(again.headers["retry-after"]) <= 60
    assert len(outbox.sent) == 1
    await _age(db, timedelta(seconds=61))
    assert (await _ask(client)).status_code == 202


async def test_codes_an_hour_are_limited_per_address(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    for _ in range(get_settings().login_codes_per_hour):
        assert (await _ask(client)).status_code == 202
    refused = await _ask(client)
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_codes"
    # Another address is not held by this one.
    assert (await _ask(client, "bob@example.org")).status_code == 202
    await _age(db, timedelta(hours=1))
    assert (await _ask(client)).status_code == 202


async def test_a_new_code_does_not_lift_the_lock(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    limits.setattr(get_settings(), "login_codes_per_hour", 0)
    for _ in range(get_settings().login_lock_failures):
        await _ask(client)
        assert (await _verify(client, _wrong(outbox.last_code()))).status_code == 401
    # Locked: neither a new code nor the right one gets in.
    refused = await _ask(client)
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_attempts"
    assert int(refused.headers["retry-after"]) <= get_settings().login_lock_minutes * 60
    locked = await _verify(client, outbox.last_code())
    assert locked.status_code == 429
    assert locked.json()["detail"] == "too_many_attempts"

    await _age(db, timedelta(minutes=get_settings().login_lock_minutes))
    assert (await _ask(client)).status_code == 202
    assert (await _verify(client, outbox.last_code())).status_code == 200


async def test_wrong_codes_a_day_are_limited(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    limits.setattr(get_settings(), "login_failures_per_day", 3)
    limits.setattr(get_settings(), "login_lock_failures", 0)
    for _ in range(3):
        await _ask(client)
        assert (await _verify(client, _wrong(outbox.last_code()))).status_code == 401
    assert (await _ask(client)).json()["detail"] == "too_many_attempts"
    await _age(db, timedelta(days=1))
    assert (await _ask(client)).status_code == 202


async def test_codes_are_limited_per_network_address(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "ip_login_codes_per_hour", 2)
    assert (await _ask(client, "a@example.org")).status_code == 202
    assert (await _ask(client, "b@example.org")).status_code == 202
    refused = await _ask(client, "c@example.org")
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_requests"


async def test_a_portal_names_its_visitor(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "ip_login_codes_per_hour", 1)
    limits.setattr(get_settings(), "portal_secret", SecretStr("portal-secret"))
    portal = {throttle.PORTAL_HEADER: "portal-secret"}
    one = {**portal, throttle.CLIENT_IP_HEADER: "203.0.113.1"}
    other = {**portal, throttle.CLIENT_IP_HEADER: "203.0.113.2"}
    assert (await _ask(client, "a@example.org", **one)).status_code == 202
    assert (await _ask(client, "b@example.org", **other)).status_code == 202
    assert (await _ask(client, "c@example.org", **one)).status_code == 429
    # Without the secret, the address named is not believed: the caller's counts.
    forged = {throttle.PORTAL_HEADER: "guess", throttle.CLIENT_IP_HEADER: "203.0.113.3"}
    assert (await _ask(client, "d@example.org", **forged)).status_code == 202
    assert (await _ask(client, "e@example.org", **forged)).status_code == 429


async def test_wrong_codes_are_limited_per_network_address(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "ip_login_failures_per_hour", 2)
    for email in ("a@example.org", "b@example.org"):
        await _ask(client, email)
        assert (await _verify(client, _wrong(outbox.last_code()), email)).status_code == 401
    await _ask(client, "c@example.org")
    refused = await _verify(client, outbox.last_code(), "c@example.org")
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_requests"


async def test_applications_are_limited_per_network_address(
    client: AsyncClient, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "ip_applications_per_hour", 1)
    body = {"issuer": "iss_nope", "type": "membership"}
    assert (await client.post("/api/v1/applications", json=body)).status_code == 404
    refused = await client.post("/api/v1/applications", json=body)
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_requests"


async def test_wallet_sign_ins_are_limited_per_network_address(
    client: AsyncClient, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "ip_wallet_requests_per_hour", 1)
    path = "/api/v1/auth/wallet/requests"
    assert (await client.post(path, json={"purpose": "sign_in"})).status_code == 200
    refused = await client.post(path, json={"purpose": "sign_in"})
    assert refused.status_code == 429
    assert refused.json()["detail"] == "too_many_requests"


async def test_a_limit_of_zero_is_none(
    client: AsyncClient, outbox: Outbox, limits: pytest.MonkeyPatch
) -> None:
    limits.setattr(get_settings(), "login_codes_per_hour", 0)
    for _ in range(10):
        assert (await _ask(client)).status_code == 202


def _request(client: str, **headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "client": (client, 1234),
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_an_ipv6_address_counts_by_its_64() -> None:
    one = throttle.client_ip(_request("2001:db8:1:2::1"))
    other = throttle.client_ip(_request("2001:db8:1:2:ffff::9"))
    assert one == other == "2001:db8:1:2::/64"
    assert throttle.client_ip(_request("2001:db8:1:3::1")) != one
    assert throttle.client_ip(_request("::ffff:198.51.100.7")) == "198.51.100.7"
    assert throttle.client_ip(_request("198.51.100.7")) == "198.51.100.7"


def test_a_runaway_pattern_fails_in_time() -> None:
    started = time.monotonic()
    assert not patterns.matches(r"(a+)+$", "a" * 40 + "!")
    assert time.monotonic() - started < 1
    assert patterns.matches(r"[A-Z]{3}\d+", "ABC123")
    assert not patterns.valid("(unclosed")
