from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import housekeeping
from registry_api.models import LoginCode, OAuthFlow, Session, Throttle, WalletRequest
from tests.conftest import Outbox
from tests.test_directory import _sign_in


async def _count(db: AsyncSession, model: type[object]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_what_ran_out_is_swept(client: AsyncClient, outbox: Outbox, db: AsyncSession) -> None:
    headers, _ = await _sign_in(client, outbox, "ada@example.org")
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=2)
    db.add_all(
        [
            LoginCode(email="old@example.org", code_hash=bytes(32), expires_at=long_ago),
            LoginCode(
                email="new@example.org", code_hash=bytes(32), expires_at=now + timedelta(minutes=5)
            ),
            OAuthFlow(
                state_hash=bytes(32),
                provider="github",
                code_verifier="v",
                nonce="n",
                expires_at=long_ago,
            ),
            WalletRequest(
                poll_hash=bytes(32), nonce="n", purpose="sign_in", locale="en", expires_at=long_ago
            ),
            WalletRequest(
                poll_hash=bytes([1]) * 32,
                nonce="n",
                purpose="sign_in",
                locale="en",
                # Just expired: kept a while, so a poll still hears it expired.
                expires_at=now - timedelta(minutes=1),
            ),
            Throttle(key="ip:codes:1.2.3.4", window_start=long_ago, hits=3),
            Throttle(key="ip:codes:5.6.7.8", window_start=now, hits=1),
        ]
    )
    await db.commit()
    # Ada's session ended long ago.
    for session in await db.scalars(select(Session)):
        session.expires_at = long_ago
    await db.commit()

    swept = await housekeeping.sweep(db)
    assert swept["login_codes"] == 1 and swept["oauth_flows"] == 1
    assert swept["wallet_requests"] == 1 and swept["throttles"] == 1
    assert swept["sessions"] >= 1
    assert await _count(db, LoginCode) == 1
    assert await _count(db, WalletRequest) == 1
    kept = set(await db.scalars(select(Throttle.key)))
    assert "ip:codes:5.6.7.8" in kept and "ip:codes:1.2.3.4" not in kept
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    # Nothing more to sweep.
    assert not any((await housekeeping.sweep(db)).values())
