"""The platform signing as a tenant: its signing flow `none` (`signing_flows`).

A tenant that chooses `none` signs nothing itself: the platform signs what it
would — its identities' did:webvh log entries and its endorsements — with an
Ed25519 key of the tenant's kept in the vault
(`tenants/{tenant_id}/signing`, a JWK), never in the database. The public half
is on `Tenant.platform_key` (a multikey): it is the log's `updateKeys` and the
key the tenant's DID document lists, as an admin's wallet's would be.

**When.** The key is made the first time the tenant chooses `none` and kept
after, so the platform can hand over to the flow chosen next. Log entries are
signed as soon as there is one to sign (`settle`: the routes that change what
an identity publishes call it after they commit); an endorsement when an
admin publishes. The entry that moves a log from wallets to the platform is
signed by the old flow's signers, as any change of flow is.
"""

import base64
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import dids, messaging_keys, webvh
from registry_api.config import get_settings
from registry_api.models import Identity, Tenant
from registry_api.vault import Vault, VaultError, paths
from registry_api.wallet import b58encode, did_key

logger = logging.getLogger(__name__)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def path(tenant_id: uuid.UUID) -> str:
    return paths.tenant_secret(tenant_id, "signing")


async def ensure(vault: Vault, tenant: Tenant) -> str:
    """The tenant's platform key, made the first time; set on the tenant (the
    caller commits). VaultError when the vault fails."""
    where = path(tenant.id)
    secret = await vault.read(where)
    if secret is None:
        private = Ed25519PrivateKey.generate()
        secret = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _b64(private.public_key().public_bytes_raw()),
            "d": _b64(private.private_bytes_raw()),
        }
        await vault.write(where, secret)
    tenant.platform_key = webvh.multikey(did_key(_unb64(secret["x"])))
    return tenant.platform_key


async def proof(
    vault: Vault,
    tenant: Tenant,
    document: dict[str, Any],
    controller: str | None = None,
    purpose: str = "assertionMethod",
) -> dict[str, Any]:
    """An `eddsa-jcs-2022` proof over `document` by the tenant's platform key,
    as a wallet makes it: a log entry's (the key named as its `did:key`), or
    with `controller` a credential's or a presentation's. VaultError when the
    vault fails or holds no key."""
    secret = await vault.read(path(tenant.id))
    if secret is None or tenant.platform_key is None:
        raise VaultError("the tenant has no platform key")
    key = tenant.platform_key
    options: dict[str, Any] = {
        "type": "DataIntegrityProof",
        "cryptosuite": webvh.CRYPTOSUITE,
        "verificationMethod": f"{controller or f'did:key:{key}'}#{key}",
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proofPurpose": purpose,
    }
    if "@context" in document:
        options["@context"] = document["@context"]
    digest = hashlib.sha256(webvh.jcs(options)).digest()
    digest += hashlib.sha256(webvh.jcs(document)).digest()
    private = Ed25519PrivateKey.from_private_bytes(_unb64(secret["d"]))
    return {**options, "proofValue": "z" + b58encode(private.sign(digest))}


async def sign_entry(db: AsyncSession, vault: Vault, identity: Identity) -> bool:
    """Signs the identity's next log entry and writes it, if the tenant's
    platform key may sign it; whether it did (the caller commits). An issuer's
    or verifier's messaging key is made first, as a wallet's signature does.
    VaultError when the vault fails."""
    tenant = await db.get_one(Tenant, identity.tenant_id)
    if tenant.platform_key is None:
        return False
    item = await messaging_keys.owner(db, identity.id)
    if item is not None and identity.agreement_key is None and tenant.signing_flow == "none":
        await messaging_keys.ensure(vault, item, identity)
    try:
        entry = await dids.next_entry(db, get_settings().did_url, identity)
    except webvh.LogError:
        return False
    if entry is None:
        return False
    entries = await dids.log(db, identity.id)
    if tenant.platform_key not in webvh.signers_for(entries, entry):
        return False
    previous = entries[-1]["versionId"] if entries else None
    await dids.append(db, identity, previous, entry, await proof(vault, tenant, entry))
    return True


async def settle(db: AsyncSession, vault: Vault, tenant_id: uuid.UUID) -> None:
    """Signs every log entry of the tenant's identities its platform key may
    sign, the tenant's own first (what it endorses is checked against it).
    Never fails what called it: with the vault down, they wait to be signed."""
    tenant = await db.get_one(Tenant, tenant_id)
    if tenant.platform_key is None:
        return
    found = await db.scalars(
        select(Identity)
        .where(Identity.tenant_id == tenant_id)
        .order_by(Identity.id != tenant.identity_id, Identity.created_at)
    )
    try:
        for identity in list(found):
            if await sign_entry(db, vault, identity):
                await db.commit()
    except (VaultError, dids.LogMoved):
        await db.rollback()
        logger.warning("could not sign as tenant %s", tenant_id, exc_info=True)
