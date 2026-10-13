"""Counting what callers do, and refusing once it is too much.

Sign-in codes are counted per address — how many are sent, and how many wrong
ones are tried, whichever code they were for — and the public requests that
cost something per network address (an IPv6 one by its /64): codes asked for,
wrong codes, applications started, wallet sign-ins asked for.

Counters are rows (`models.Throttle`), one per key and fixed window, so every
replica of the API sees the same ones. A refusal is 429 with `Retry-After`.

The portals call this API from their servers, so the address it sees is
theirs: one that sends the shared `REGISTRY_PORTAL_SECRET` (`X-Almena-Portal`)
names its visitor's instead (`X-Almena-Client-IP`).
"""

import hmac
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address, ip_network

from fastapi import HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.config import get_settings
from registry_api.models import Throttle

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
PORTAL_HEADER = "x-almena-portal"
CLIENT_IP_HEADER = "x-almena-client-ip"


@dataclass(frozen=True)
class Limit:
    key: str
    # At most this many in a window; 0 lifts the limit.
    most: int
    window: timedelta


def _utc(moment: datetime) -> datetime:
    # PostgreSQL hands back aware datetimes; SQLite (the tests) naive UTC ones.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def refusal(code: str, retry: timedelta) -> HTTPException:
    seconds = max(1, math.ceil(retry.total_seconds()))
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail=code, headers={"Retry-After": str(seconds)}
    )


async def check(db: AsyncSession, code: str, *limits: Limit) -> None:
    """Refuse with `code` when any of `limits` is already spent."""
    now = datetime.now(UTC)
    for limit in limits:
        if limit.most <= 0:
            continue
        row = await db.get(Throttle, limit.key, populate_existing=True)
        if row is None:
            continue
        ends = _utc(row.window_start) + limit.window
        if ends > now and row.hits >= limit.most:
            raise refusal(code, ends - now)


async def count(db: AsyncSession, *limits: Limit) -> None:
    """One more against each of `limits`; the caller commits."""
    now = datetime.now(UTC)
    insert = postgresql_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    for limit in limits:
        if limit.most <= 0:
            continue
        added = await db.execute(
            insert(Throttle)
            .values(key=limit.key, window_start=now, hits=1)
            .on_conflict_do_nothing(index_elements=["key"])
        )
        if getattr(added, "rowcount", 0):
            # A new counter (old ones are swept by `registry_api.housekeeping`).
            continue
        row = await db.get(Throttle, limit.key, populate_existing=True)
        if row is None:
            continue
        if _utc(row.window_start) + limit.window <= now:
            row.window_start, row.hits = now, 1
        else:
            row.hits += 1
    await db.flush()


async def hit(db: AsyncSession, code: str, *limits: Limit) -> None:
    """Refuse with `code` when any of `limits` is spent, else count one more
    and commit it: the request counts whatever comes of it."""
    await check(db, code, *limits)
    await count(db, *limits)
    await db.commit()


def client_ip(request: Request) -> str:
    """The network address asking: the visitor a portal names, when the portal
    proves it is one, else the caller (behind the trusted proxies). An IPv6
    address stands for its /64, which one machine usually has whole."""
    secret = get_settings().portal_secret.get_secret_value()
    named = request.headers.get(CLIENT_IP_HEADER)
    given = request.headers.get(PORTAL_HEADER, "")
    raw = request.client.host if request.client else ""
    if secret and named and hmac.compare_digest(given.encode(), secret.encode()):
        raw = named.strip()
    try:
        address = ip_address(raw)
    except ValueError:
        return raw or "unknown"
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ip_network(f"{address}/64", strict=False))
    return str(address)


def per_ip(action: str, request: Request, most: int) -> Limit:
    """`action` (`codes`, `failures`, `applications`, `wallet`) from one
    network address, `most` an hour."""
    return Limit(f"ip:{action}:{client_ip(request)}", most, HOUR)
