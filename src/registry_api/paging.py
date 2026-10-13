"""Keyset paging, newest first: a page of `limit` and the cursor for the next.

A cursor is opaque to callers: the sort key of the last item seen (a moment
and an id, and whatever else breaks ties), base64url-encoded. A page holds
what comes strictly after it, so items added meanwhile never repeat one.
"""

import base64
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import HTTPException, Query, status
from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=300)]


def encode(*parts: object) -> str:
    """A cursor holding `parts` (moments as ISO 8601)."""
    raw = "|".join(part.isoformat() if isinstance(part, datetime) else str(part) for part in parts)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor: str, count: int) -> list[str]:
    """The `count` parts of a cursor; `400 invalid_cursor` for anything else."""
    try:
        parts = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split("|")
    except ValueError:
        raise invalid() from None
    if len(parts) != count:
        raise invalid()
    return parts


def position(cursor: str) -> tuple[datetime, uuid.UUID]:
    """A cursor's moment and id."""
    moment, item_id = decode(cursor, 2)
    try:
        return datetime.fromisoformat(moment), uuid.UUID(item_id)
    except ValueError:
        raise invalid() from None


def invalid() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_cursor")


def after(moment_column: Any, id_column: Any, cursor: str) -> ColumnElement[bool]:
    """What comes after the cursor in (moment, id) order, newest first."""
    return beyond(moment_column, id_column, *position(cursor))


def beyond(
    moment_column: Any, id_column: Any, moment: datetime, item_id: uuid.UUID
) -> ColumnElement[bool]:
    """What comes after (`moment`, `item_id`), newest first."""
    return or_(moment_column < moment, and_(moment_column == moment, id_column < item_id))
