"""What goes into an issuer's queue at the broker (`registry_api.broker`), for
its back office: what happens to the applications it receives.

Each message is JSON, its AMQP `type` the same as its `type` field:

- `application.submitted` — a holder signed and sent an application: what
  they signed (`content`), its `digest` and `signature`, the credentials
  presented, and each file with where the API serves it (an API token of the
  tenant downloads it);
- `application.decided` — accepted or rejected, with the note;
- `application.issued` — the credential was issued (`valid_until`);
- `credential.status` — an issued credential suspended, reinstated or revoked.

A verifier's queue hears its verifications by QR (`routes.verifications`):

- `presentation.verified` — a wallet answered: the verdict (`verified`, and
  each credential's key, issuer, claims, the form's fields it fills and its
  problems), with the verification (`id`, `form_id`, `form`, `answered_at`).

An issuer's messages name the issuer (`id`, `slug`, `did`) and the
application (`id`, `slug`, `status`, `credential_type`, `form_id`,
`holder_did`); a verifier's, the verifier (`id`, `slug`, `did`).

Publishing is never a condition: with no queue nothing is sent, and when the
broker does not take it what happened stands and the failure is logged.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.broker import Broker, BrokerError
from registry_api.config import get_settings
from registry_api.models import Application, Issuer, Verifier

logger = logging.getLogger(__name__)


def _when(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def application_of(item: Application) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "slug": item.slug,
        "status": item.status,
        "credential_type": item.credential_type,
        "form_id": str(item.form_id),
        "holder_did": item.holder_did,
        "submitted_at": _when(item.submitted_at),
    }


def file_url(item: Application, tenant_id: str, key: str) -> str:
    base = get_settings().public_url.rstrip("/")
    return f"{base}/api/v1/tenants/{tenant_id}/applications/{item.id}/files/{key}"


async def publish(
    db: AsyncSession, broker: Broker, item: Application, kind: str, extra: dict[str, Any]
) -> bool:
    """Puts `kind` about `item` in its issuer's queue, if it has one; whether
    the broker took it."""
    issuer = await db.get(Issuer, item.issuer_id)
    if issuer is None or issuer.queue_created_at is None:
        return False
    message = {
        "type": kind,
        "at": datetime.now(UTC).isoformat(),
        "issuer": {"id": str(issuer.id), "slug": issuer.slug, "did": issuer.identity.did},
        "application": application_of(item),
        **extra,
    }
    try:
        await broker.publish(issuer.queue_name, kind, message)
    except BrokerError as error:
        logger.warning(
            "queue_publish_failed",
            extra={"application_id": str(item.id), "type": kind, "error": str(error)},
        )
        return False
    return True


async def submitted(
    db: AsyncSession,
    broker: Broker,
    item: Application,
    content: dict[str, Any],
    files: dict[str, dict[str, Any]],
) -> bool:
    tenant = str((await db.get_one(Issuer, item.issuer_id)).tenant_id)
    return await publish(
        db,
        broker,
        item,
        "application.submitted",
        {
            "content": content,
            "digest": item.digest,
            "signature": item.signature,
            "presented": item.presented,
            "files": {
                key: {**meta, "url": file_url(item, tenant, key)} for key, meta in files.items()
            },
        },
    )


async def decided(db: AsyncSession, broker: Broker, item: Application) -> bool:
    return await publish(
        db,
        broker,
        item,
        "application.decided",
        {"decided_at": _when(item.decided_at), "note": item.decision_note},
    )


async def issued(db: AsyncSession, broker: Broker, item: Application) -> bool:
    return await publish(
        db,
        broker,
        item,
        "application.issued",
        {
            "issued_at": _when(item.issued_at),
            "valid_until": _when(item.credential_valid_until),
        },
    )


async def status_changed(db: AsyncSession, broker: Broker, item: Application) -> bool:
    return await publish(
        db,
        broker,
        item,
        "credential.status",
        {
            "credential_status": item.credential_status,
            "changed_at": _when(item.credential_status_at),
        },
    )


async def presentation_verified(broker: Broker, verifier: Verifier, extra: dict[str, Any]) -> bool:
    """Puts a verification's verdict in the verifier's queue, if it has one."""
    if verifier.queue_created_at is None:
        return False
    kind = "presentation.verified"
    message = {
        "type": kind,
        "at": datetime.now(UTC).isoformat(),
        "verifier": {"id": str(verifier.id), "slug": verifier.slug, "did": verifier.identity.did},
        **extra,
    }
    try:
        await broker.publish(verifier.queue_name, kind, message)
    except BrokerError as error:
        logger.warning(
            "queue_publish_failed",
            extra={"verifier_id": str(verifier.id), "type": kind, "error": str(error)},
        )
        return False
    return True
