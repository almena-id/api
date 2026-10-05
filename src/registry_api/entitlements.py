"""What a tenant's subscription lets it do.

The platform is open to everyone; a subscription — paid by the month, per
tenant (`registry_api.models.Subscription`) — adds features. Each is named
here, each plan lists the ones it gives, and `allows` is the one place that
decides; routes ask it and refuse with `403 subscription_required`, and the
tenant's features are listed with it (`GET /tenants`) so the portal offers
only what it may. The trust anchor has every feature, always.

A subscription gives its plan's features while `active`, and while
`past_due` for `GRACE` past the end of its period; `canceled`, or none, gives
none. What a tenant made while subscribed keeps working without one: only
making and changing it again needs the feature.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Subscription, Tenant

Feature = Literal["own_fields", "own_credential_types"]
FEATURES: tuple[Feature, ...] = ("own_fields", "own_credential_types")

# The plans a subscription may be on, and what each gives. One so far; its
# price is not settled.
PLANS: dict[str, tuple[Feature, ...]] = {"standard": FEATURES}

# How long a missed payment leaves the features in place.
GRACE = timedelta(days=7)


def as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def in_force(item: Subscription | None, now: datetime | None = None) -> bool:
    """Whether it gives its plan's features now."""
    if item is None or item.status not in ("active", "past_due"):
        return False
    if item.current_period_end is None:
        return True
    end = as_utc(item.current_period_end)
    if item.status == "past_due":
        end += GRACE
    return (now or datetime.now(UTC)) < end


async def features(db: AsyncSession, tenant: Tenant) -> list[Feature]:
    """The features `tenant` may use."""
    if tenant.root:
        return list(FEATURES)
    item = await db.get(Subscription, tenant.id)
    return list(PLANS.get(item.plan, ())) if item and in_force(item) else []


async def allows(db: AsyncSession, tenant: Tenant, feature: Feature) -> bool:
    """Whether `tenant` may use `feature`."""
    return feature in await features(db, tenant)
