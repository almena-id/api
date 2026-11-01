"""A tenant's activity, for its overview: what its issuers and verifiers did
day by day over a window (applications submitted and decided, credentials
issued, presentations verified) and how things stand now — the applications by
status, the credentials issued by status, the verifications by verdict. Any
member reads it; nothing here is kept apart from what it counts."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import Application, Verification

router = APIRouter(prefix="/tenants/{tenant_id}/activity", tags=["activity"])


class Day(BaseModel):
    # The UTC day.
    date: date
    submitted: int
    accepted: int
    rejected: int
    issued: int
    verified: int
    # Presentations that did not hold.
    failed: int


class Applications(BaseModel):
    """Applications received, by where they stand now (never the ones still
    being filled in)."""

    submitted: int
    accepted: int
    rejected: int
    issued: int


class Credentials(BaseModel):
    """Credentials issued, by their status now."""

    valid: int
    suspended: int
    revoked: int


class Verifications(BaseModel):
    """Presentations answered, by verdict."""

    verified: int
    failed: int


class Activity(BaseModel):
    days: list[Day]
    applications: Applications
    credentials: Credentials
    verifications: Verifications


async def _grouped(db: AsyncSession, query: Select[Any, int]) -> dict[Any, int]:
    """How many rows there are of each value, by value."""
    return {value: count for value, count in await db.execute(query)}


def _day(moment: datetime) -> date:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).date()


@router.get(
    "", summary="What the tenant's issuers and verifiers did, day by day, and where it stands"
)
async def get_activity(
    tenant_id: TenantId,
    db: DbSession,
    # How many UTC days, today the last of them.
    days: Annotated[int, Query(ge=7, le=90)] = 30,
) -> Activity:
    today = datetime.now(UTC).date()
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, time(), UTC)
    counts = {
        first + timedelta(days=n): dict.fromkeys(
            ("submitted", "accepted", "rejected", "issued", "verified", "failed"), 0
        )
        for n in range(days)
    }

    def count(moment: datetime | None, what: str) -> None:
        if moment is not None and _day(moment) in counts:
            counts[_day(moment)][what] += 1

    received = await db.execute(
        select(
            Application.status,
            Application.submitted_at,
            Application.decided_at,
            Application.issued_at,
        ).where(
            Application.tenant_id == tenant_id,
            (Application.submitted_at >= since)
            | (Application.decided_at >= since)
            | (Application.issued_at >= since),
        )
    )
    for state, submitted_at, decided_at, issued_at in received:
        count(submitted_at, "submitted")
        # An issued application was accepted first.
        count(decided_at, "rejected" if state == "rejected" else "accepted")
        count(issued_at, "issued")
    answered = await db.execute(
        select(Verification.verified, Verification.answered_at).where(
            Verification.tenant_id == tenant_id, Verification.answered_at >= since
        )
    )
    for verified, answered_at in answered:
        count(answered_at, "verified" if verified else "failed")

    by_status = await _grouped(
        db,
        select(Application.status, func.count())
        .where(Application.tenant_id == tenant_id)
        .group_by(Application.status),
    )
    issued = await _grouped(
        db,
        select(Application.credential_status, func.count())
        .where(Application.tenant_id == tenant_id, Application.status == "issued")
        .group_by(Application.credential_status),
    )
    verdicts = await _grouped(
        db,
        select(Verification.verified, func.count())
        .where(Verification.tenant_id == tenant_id, Verification.answered_at.is_not(None))
        .group_by(Verification.verified),
    )
    return Activity(
        days=[Day(date=day, **counted) for day, counted in counts.items()],
        applications=Applications(
            submitted=by_status.get("submitted", 0),
            accepted=by_status.get("accepted", 0),
            rejected=by_status.get("rejected", 0),
            issued=by_status.get("issued", 0),
        ),
        credentials=Credentials(
            # Issued and never changed: valid.
            valid=issued.get(None, 0) + issued.get("valid", 0),
            suspended=issued.get("suspended", 0),
            revoked=issued.get("revoked", 0),
        ),
        verifications=Verifications(verified=verdicts.get(True, 0), failed=verdicts.get(False, 0)),
    )
