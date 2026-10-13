"""Counters of what callers do, one per key and window (registry_api.throttle)."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from registry_api.models.base import Base


class Throttle(Base):
    __tablename__ = "throttles"

    # What is counted and for whom: `code:{email}`, `ip:applications:{address}`…
    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    # When the current window started; past it, the count starts again.
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    hits: Mapped[int] = mapped_column(Integer)
