"""Sweeping what has run out, so it does not pile up: sign-in codes, provider
sign-ins and account moves, ended sessions, wallet requests, applications
started and never sent (their files with them), and old throttle counters.

`sweep` runs every `REGISTRY_HOUSEKEEPING_MINUTES` in each API process
(`run`, started by the app's lifespan); deleting what has run out twice is
harmless, so replicas need not agree on who does it. Whatever expired is
already refused where it is used: this only frees the space.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry_api.config import get_settings
from registry_api.models import (
    AccountMove,
    Application,
    LoginCode,
    OAuthFlow,
    Session,
    Throttle,
    WalletRequest,
)
from registry_api.throttle import DAY

logger = logging.getLogger(__name__)

# Past their end, kept a little longer: whoever polls one just expired is
# still told it expired rather than that it never was.
GRACE = timedelta(hours=1)


async def sweep(db: AsyncSession) -> dict[str, int]:
    """Delete what has run out; how many of each."""
    settings = get_settings()
    now = datetime.now(UTC)
    idle = now - timedelta(minutes=settings.session_idle_minutes)
    statements = {
        "login_codes": delete(LoginCode).where(LoginCode.expires_at <= now),
        "oauth_flows": delete(OAuthFlow).where(OAuthFlow.expires_at <= now - GRACE),
        "account_moves": delete(AccountMove).where(AccountMove.expires_at <= now - GRACE),
        "sessions": delete(Session).where(
            (Session.expires_at <= now) | (Session.name.is_(None) & (Session.last_seen_at <= idle))
        ),
        "wallet_requests": delete(WalletRequest).where(WalletRequest.expires_at <= now - GRACE),
        # Never sent: what the holder answered and uploaded goes with them.
        "applications": delete(Application).where(
            Application.status.in_(("open", "paired")), Application.expires_at <= now
        ),
        "throttles": delete(Throttle).where(Throttle.window_start < now - DAY),
    }
    counts = {}
    for name, statement in statements.items():
        result = await db.execute(statement)
        counts[name] = int(getattr(result, "rowcount", 0) or 0)
    await db.commit()
    return counts


async def run(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Sweep now and then, until cancelled; a failed sweep waits for the next."""
    minutes = get_settings().housekeeping_minutes
    if minutes <= 0:
        return
    while True:
        try:
            async with sessions() as db:
                swept = await sweep(db)
            if any(swept.values()):
                logger.info("housekeeping", extra={"swept": swept})
        except Exception:
            logger.exception("housekeeping_failed")
        await asyncio.sleep(minutes * 60)
