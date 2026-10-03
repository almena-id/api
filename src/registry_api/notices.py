"""Telling a holder how their application stands, over DIDComm.

When an issuer decides on an application (accepted or rejected) and when it
issues the credential, the holder's wallet gets a message from the issuer
(`https://almena.id/protocols/application/1.0/status`) at the `did:peer:2` it
named when pairing (`Application.holder_messaging_did`):

    {"application": <id>, "status": "accepted" | "rejected" | "issued",
     "credential_type": <type id>, "credential_name": {<lang>: <name>},
     "issuer": <issuer name>,
     "note": <the decision's note, when there is one>,
     "collect": <where the wallet asks to receive it, once issued>}

`thid` is the application's id, so every notice about it is one thread. The
message comes from the issuer's DID under its `did:web` name (the form a
wallet's messaging resolves) with the X25519 key the vault keeps for it.

A notice is a courtesy, never a condition: when the holder named no DID, the
issuer has no messaging key yet, or the mediator cannot be reached, the
decision or issuance stands and the failure is logged.
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import credential_catalog as credentials
from registry_api import didcomm, messaging_keys
from registry_api.broker import Broker, get_broker
from registry_api.config import get_settings
from registry_api.models import Application, Issuer
from registry_api.vault import Vault, VaultError, get_vault

logger = logging.getLogger(__name__)

STATUS = "https://almena.id/protocols/application/1.0/status"
Status = Literal["accepted", "rejected", "issued"]


def did_web(did: str) -> str:
    """The `did:web` name of a registry DID: `did:webvh:{SCID}:host…` is
    `did:web:host…`; anything else is already its own name."""
    if did.startswith("did:webvh:"):
        return "did:web:" + did.split(":", 3)[3]
    return did


def collect_url(item: Application) -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/v1/applications/{item.id}/collect"


async def notify(
    db: AsyncSession,
    vault: Vault,
    courier: didcomm.Courier,
    item: Application,
    status: Status,
) -> bool:
    """Sends the holder the notice of `status`; whether it was handed to their
    mediator."""
    if not item.holder_messaging_did:
        return False
    issuer = await db.get(Issuer, item.issuer_id)
    if issuer is None or issuer.identity.did is None or issuer.identity.agreement_key is None:
        return False
    sender = did_web(issuer.identity.did)
    body: dict[str, object] = {
        "application": str(item.id),
        "status": status,
        "credential_type": item.credential_type,
        "credential_name": credentials.BY_ID[item.credential_type].labels,
        "issuer": issuer.name,
    }
    if item.decision_note and status != "issued":
        body["note"] = item.decision_note
    if status == "issued":
        body["collect"] = collect_url(item)
    plain = didcomm.message(
        STATUS, body, sender=sender, to=item.holder_messaging_did, thid=str(item.id)
    )
    try:
        key = await messaging_keys.private(vault, issuer)
        if key is None:
            return False
        await courier.deliver(
            plain,
            sender_kid=f"{sender}#{issuer.identity.agreement_key}",
            sender_key=key,
            to=item.holder_messaging_did,
        )
    except (didcomm.DidCommError, VaultError) as error:
        logger.warning(
            "holder_notice_failed",
            extra={"application_id": str(item.id), "status": status, "error": str(error)},
        )
        return False
    logger.info("holder_notified", extra={"application_id": str(item.id), "status": status})
    return True


@dataclass(frozen=True)
class Notifier:
    """What telling others needs — the vault and the courier for the holder,
    the broker for the issuer's queue (`registry_api.queues`) — carried down
    to where something happens."""

    vault: Vault
    courier: didcomm.Courier
    broker: Broker

    async def notify(self, db: AsyncSession, item: Application, status: Status) -> bool:
        return await notify(db, self.vault, self.courier, item, status)


Courier = Annotated[didcomm.Courier, Depends(didcomm.get_courier)]
VaultDep = Annotated[Vault, Depends(get_vault)]
BrokerDep = Annotated[Broker, Depends(get_broker)]
