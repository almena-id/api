"""Issuers' status lists: whether the credentials they issued still hold.

Each issuer keeps IETF Token Status Lists (draft-ietf-oauth-status-list): a
JWT (`typ: statuslist+jwt`) whose `status_list` packs `BITS` bits per
credential — `0` valid, `1` revoked (final), `2` suspended — compressed with
DEFLATE (zlib) and base64url-encoded, public at `{public_url}/status-lists/{slug}`.
Every SD-JWT VC an issuer issues names its entry (`status.status_list`: `idx`
and `uri`), drawn at random among the list's free ones so a credential's
index says nothing of when it was issued nor of its neighbours.

Nobody signs on the server: the list is signed, like the credentials, by the
issuer's signer's wallet, with a key its DID lists (`assertionMethod`). A
change of status is a new list to sign, kept only once signed — and only if
no other signature replaced the list meanwhile (`revision`). A list is signed
once before the first credential that names it is issued, and again whenever
the key it was signed with is no longer one the DID lists.
"""

import base64
import secrets
import zlib
from datetime import UTC, datetime
from typing import Any

import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import presentations
from registry_api.config import get_settings
from registry_api.models import Application, Issuer, StatusList

BITS = 2
# Statuses a list holds: 2^17, 32 KiB before compression (a few dozen bytes
# after, while mostly valid).
SIZE = 2**17
STATUSES = {"valid": 0, "revoked": 1, "suspended": 2}
NAMES = {value: name for name, value in STATUSES.items()}
_PER_BYTE = 8 // BITS
_MASK = (1 << BITS) - 1


def next_statuses(now: str | None) -> list[str]:
    """The statuses a credential may be given from `now` (`null`: valid, as
    issued): revoking is final; a suspended one is reinstated or revoked."""
    now = now or "valid"
    if now == "revoked":
        return []
    return ["valid", "revoked"] if now == "suspended" else ["suspended", "revoked"]


def uri(item: StatusList) -> str:
    return f"{get_settings().public_url.rstrip('/')}/status-lists/{item.slug}"


def empty(size: int = SIZE) -> bytes:
    return bytes(size // _PER_BYTE)


def value_at(data: bytes, index: int) -> int:
    """The status at `index`: its bits, from the least significant ones of
    each byte up, as the draft packs them."""
    return (data[index // _PER_BYTE] >> ((index % _PER_BYTE) * BITS)) & _MASK


def with_value(data: bytes, index: int, value: int) -> bytes:
    changed = bytearray(data)
    shift = (index % _PER_BYTE) * BITS
    at = index // _PER_BYTE
    changed[at] = (changed[at] & ~(_MASK << shift) & 0xFF) | (value << shift)
    return bytes(changed)


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(zlib.compress(data, 9)).decode().rstrip("=")


def decode(lst: str) -> bytes:
    return zlib.decompress(base64.urlsafe_b64decode(lst + "=" * (-len(lst) % 4)))


def _tally(value: int) -> bytes:
    """For each byte, how many of its statuses are `value`."""
    return bytes(
        sum(1 for slot in range(_PER_BYTE) if (byte >> (slot * BITS)) & _MASK == value)
        for byte in range(256)
    )


_TALLIES = {name: _tally(value) for name, value in STATUSES.items() if value}


def counts(data: bytes) -> dict[str, int]:
    """How many statuses are revoked and how many suspended."""
    return {name: sum(data.translate(table)) for name, table in _TALLIES.items()}


def document(item: StatusList, data: bytes, issuer: str, key: str) -> dict[str, Any]:
    """What the issuer's signer signs: the Status List Token's header and payload."""
    return {
        "header": {"alg": "EdDSA", "typ": "statuslist+jwt", "kid": f"{issuer}#{key}"},
        "payload": {
            "sub": uri(item),
            "iat": int(datetime.now(UTC).timestamp()),
            "status_list": {"bits": BITS, "lst": encode(data)},
        },
    }


def signed_key(item: StatusList) -> str | None:
    """The key the list's token was signed with (its `kid`'s fragment)."""
    if item.token is None:
        return None
    try:
        return str(jwt.get_unverified_header(item.token).get("kid", "")).rpartition("#")[2]
    except jwt.PyJWTError:
        return None


async def needs_signing(db: AsyncSession, item: StatusList, issuer: Issuer) -> bool:
    """Whether the list must be signed (again) for its credentials to be
    checked: never signed yet, or by a key the issuer's DID no longer lists."""
    key = signed_key(item)
    if key is None or issuer.identity.did is None:
        return True
    listed = {name for name, _ in await presentations.issuer_keys(db, issuer.identity.did)}
    return key not in listed


async def used(db: AsyncSession, item: StatusList) -> set[int]:
    taken = await db.scalars(
        select(Application.status_index).where(Application.status_list_id == item.id)
    )
    return {index for index in taken if index is not None}


async def lists_of(db: AsyncSession, issuer: Issuer) -> list[StatusList]:
    found = await db.scalars(
        select(StatusList).where(StatusList.issuer_id == issuer.id).order_by(StatusList.created_at)
    )
    return list(found)


async def current(db: AsyncSession, issuer: Issuer) -> StatusList | None:
    """The issuer's list new credentials go in: its latest, if it has room left."""
    lists = await lists_of(db, issuer)
    if not lists:
        return None
    latest = lists[-1]
    taken = await db.scalar(select(func.count()).where(Application.status_list_id == latest.id))
    return latest if (taken or 0) < latest.size else None


async def open_list(db: AsyncSession, issuer: Issuer) -> StatusList:
    """The issuer's current list, or a new one (unsigned) if it has none."""
    found = await current(db, issuer)
    if found is not None:
        return found
    item = StatusList(
        tenant_id=issuer.tenant_id, issuer_id=issuer.id, size=SIZE, statuses=empty(SIZE)
    )
    db.add(item)
    await db.flush()
    return item


async def reserve(db: AsyncSession, application: Application, item: StatusList) -> int:
    """The application's index in `item`: the one it already holds, or a free
    one drawn at random."""
    if application.status_list_id == item.id and application.status_index is not None:
        return application.status_index
    taken = await used(db, item)
    if len(taken) * 2 < item.size:
        index = secrets.randbelow(item.size)
        while index in taken:
            index = secrets.randbelow(item.size)
    else:
        free = [index for index in range(item.size) if index not in taken]
        index = free[secrets.randbelow(len(free))]
    application.status_list_id = item.id
    application.status_index = index
    return index
