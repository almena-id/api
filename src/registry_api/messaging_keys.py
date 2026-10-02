"""Issuers' and verifiers' messaging keys: X25519, for DIDComm.

Messages to an issuer or a verifier are encrypted to the key its DID document
lists under `keyAgreement`; the platform opens them on its behalf, so it holds
the private key — in the vault, never in the database
(`tenants/{tenant_id}/{issuers|verifiers}/{id}/messaging`, a JWK). The public
half, as a multikey, is kept on its identity (`Identity.agreement_key`): the
document is built from it.

**When.** The key is made when its signer signs the element: the first request
to sign its identity (`POST /tenants/{id}/identities/{id}/sign`) makes it, so
the entry that wallet signs is already the one that lists it. Until then the
document says nothing about messaging. Asking again reuses it: the vault is
read first, and a key there is never replaced.

Mediators keep their own keys (the mediator's `keys.json`); the tenant's own
identity has none yet.
"""

import base64
import uuid
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Identity, Issuer, Verifier
from registry_api.vault import Vault, paths
from registry_api.wallet import b58encode

# Multicodec x25519-pub, as multikeys start (`z6LS…`).
_X25519 = b"\xec\x01"

Kind = Literal["issuers", "verifiers"]


def multikey(public_key: bytes) -> str:
    return "z" + b58encode(_X25519 + public_key)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


async def owner(db: AsyncSession, identity_id: uuid.UUID) -> Issuer | Verifier | None:
    """The issuer or verifier acting as the identity: those that have a key."""
    return await db.scalar(
        select(Issuer).where(Issuer.identity_id == identity_id)
    ) or await db.scalar(select(Verifier).where(Verifier.identity_id == identity_id))


def kind_of(item: Issuer | Verifier) -> Kind:
    return "issuers" if isinstance(item, Issuer) else "verifiers"


def path(tenant_id: uuid.UUID, kind: Kind, item_id: uuid.UUID) -> str:
    return paths.entity(tenant_id, kind, item_id, "messaging")


async def ensure(vault: Vault, item: Issuer | Verifier, identity: Identity) -> str:
    """The item's public key, made the first time; set on its identity (the
    caller commits). VaultError when the vault fails."""
    where = path(item.tenant_id, kind_of(item), item.id)
    secret = await vault.read(where)
    if secret is None:
        private = X25519PrivateKey.generate()
        secret = {
            "kty": "OKP",
            "crv": "X25519",
            "x": _b64(private.public_key().public_bytes_raw()),
            "d": _b64(private.private_bytes_raw()),
        }
        await vault.write(where, secret)
    identity.agreement_key = multikey(_unb64(secret["x"]))
    return identity.agreement_key


async def forget(vault: Vault, tenant_id: uuid.UUID, kind: Kind, item_id: uuid.UUID) -> None:
    """Delete every secret the item has in the vault (it is being deleted)."""
    await vault.delete_tree(paths.entity_root(tenant_id, kind, item_id))
