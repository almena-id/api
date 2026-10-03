"""An issuer's or a verifier's queue at the broker: what its back office reads.

Admins make it, and with it its user — named like the issuer or verifier
(its slug), allowed to read that queue and nothing else; its password is
shown once, when made or when it is changed. Deleting the queue deletes what
is in it, and the user. Drafts and published ones alike may have one. What
goes into it is in `registry_api.queues`.
"""

import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.api.routes.members import admin_of
from registry_api.broker import Broker, BrokerError, get_broker
from registry_api.config import get_settings
from registry_api.models import Issuer, Verifier

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["queues"])

AdminTenant = Annotated[uuid.UUID, Depends(admin_of)]
BrokerDep = Annotated[Broker, Depends(get_broker)]
Kind = Literal["issuers", "verifiers"]
MODELS: dict[str, type[Issuer] | type[Verifier]] = {"issuers": Issuer, "verifiers": Verifier}


class QueueOut(BaseModel):
    # `subject.{slug}`; `null` while it has none.
    queue: str | None
    # Who reads it: the issuer's or verifier's slug.
    user: str
    # Where its back office connects: the broker's AMQP address and vhost.
    amqp_url: str
    vhost: str
    created_at: datetime | None


class QueueAccess(QueueOut):
    # Shown this once: the broker keeps it, the registry does not.
    password: str


async def _item(
    db: DbSession, kind: Kind, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Issuer | Verifier:
    item = cast(Issuer | Verifier | None, await db.get(MODELS[kind], item_id))
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{kind[:-1]}_not_found")
    return item


def _out(item: Issuer | Verifier) -> QueueOut:
    settings = get_settings()
    return QueueOut(
        queue=item.queue_name if item.queue_created_at else None,
        user=item.slug,
        amqp_url=settings.rabbitmq_amqp_url,
        vhost=settings.rabbitmq_vhost,
        created_at=item.queue_created_at,
    )


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="broker_unavailable")


async def _grant(broker: Broker, item: Issuer | Verifier) -> str:
    password = secrets.token_urlsafe(24)
    try:
        await broker.declare(item.queue_name)
        await broker.grant(item.slug, password, item.queue_name)
    except BrokerError:
        raise _unavailable() from None
    return password


@router.get(
    "/{kind}/{item_id}/queue",
    summary="An issuer's or verifier's queue: whether it has one, and how to read it",
    responses={404: {"description": "`issuer_not_found`, `verifier_not_found`"}},
)
async def get_queue(tenant_id: TenantId, kind: Kind, item_id: uuid.UUID, db: DbSession) -> QueueOut:
    return _out(await _item(db, kind, tenant_id, item_id))


@router.post(
    "/{kind}/{item_id}/queue",
    status_code=status.HTTP_201_CREATED,
    summary="Make its queue and the user that reads it (admins); the password, once",
    responses={
        409: {"description": "`queue_exists`"},
        503: {"description": "`broker_unavailable`"},
    },
)
async def create_queue(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession, broker: BrokerDep
) -> QueueAccess:
    item = await _item(db, kind, tenant_id, item_id)
    if item.queue_created_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="queue_exists")
    password = await _grant(broker, item)
    item.queue_created_at = datetime.now(UTC)
    await db.commit()
    return QueueAccess(**_out(item).model_dump(), password=password)


@router.post(
    "/{kind}/{item_id}/queue/access",
    summary="Give its queue's user a new password (admins); the old one stops working",
    responses={
        404: {"description": "`queue_not_found`"},
        503: {"description": "`broker_unavailable`"},
    },
)
async def rotate_access(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession, broker: BrokerDep
) -> QueueAccess:
    item = await _item(db, kind, tenant_id, item_id)
    if item.queue_created_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="queue_not_found")
    password = await _grant(broker, item)
    return QueueAccess(**_out(item).model_dump(), password=password)


@router.delete(
    "/{kind}/{item_id}/queue",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete its queue, with what is in it, and its user (admins)",
    responses={
        404: {"description": "`queue_not_found`"},
        503: {"description": "`broker_unavailable`"},
    },
)
async def delete_queue(
    tenant_id: AdminTenant, kind: Kind, item_id: uuid.UUID, db: DbSession, broker: BrokerDep
) -> Response:
    item = await _item(db, kind, tenant_id, item_id)
    if item.queue_created_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="queue_not_found")
    try:
        await broker.remove(item.slug, item.queue_name)
    except BrokerError:
        raise _unavailable() from None
    item.queue_created_at = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
