"""What a request reads more than once, kept for the rest of it.

A list page asks the same things of every row — who may sign the tenant's
identities, the catalogue, the anchor — and each answer is kept in the
session's `info` (one session per request) under a key of its own. Anything
the session writes forgets them all: a flush, a commit or rollback, or an
insert, update or delete run directly; so a value kept is never older than
the last write of the request.
"""

from collections.abc import Awaitable, Callable, Hashable
from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session

_KEY = "registry_api.memo"


def _forget(session: Session, *_: Any) -> None:
    session.info.pop(_KEY, None)


def _on_execute(state: ORMExecuteState) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        _forget(state.session)


event.listen(Session, "after_flush", _forget)
event.listen(Session, "after_commit", _forget)
event.listen(Session, "after_rollback", _forget)
event.listen(Session, "do_orm_execute", _on_execute)


def kept(db: AsyncSession) -> dict[Hashable, Any]:
    """The request's kept answers (the session's)."""
    return cast(dict[Hashable, Any], db.sync_session.info.setdefault(_KEY, {}))


async def remembered[T](db: AsyncSession, key: Hashable, compute: Callable[[], Awaitable[T]]) -> T:
    """The answer kept under `key`, computed the first time it is asked."""
    store = kept(db)
    if key not in store:
        store[key] = await compute()
    return cast(T, store[key])
