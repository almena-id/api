"""Where each secret lives in the vault, by the tenant it belongs to.

Ids, never slugs: they never change and are not public.
"""

import uuid
from typing import Literal

from registry_api.vault.base import check_path

Kind = Literal["issuers", "verifiers", "mediators"]


def tenant(tenant_id: uuid.UUID) -> str:
    """Everything a tenant has in the vault hangs from here."""
    return f"tenants/{tenant_id}"


def entity_root(tenant_id: uuid.UUID, kind: Kind, entity_id: uuid.UUID) -> str:
    """Everything an issuer, verifier or mediator has in the vault hangs from here."""
    return check_path(f"{tenant(tenant_id)}/{kind}/{entity_id}")


def entity(tenant_id: uuid.UUID, kind: Kind, entity_id: uuid.UUID, name: str) -> str:
    """One of an issuer's, verifier's or mediator's secrets (`messaging`, say)."""
    return check_path(f"{entity_root(tenant_id, kind, entity_id)}/{name}")
