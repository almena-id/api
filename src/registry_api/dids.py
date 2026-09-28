"""The DIDs of the tenant's identities: `did:web` under the identity domain.

An identity with slug ``idn_…`` is ``did:web:{host}:ids:idn_…``, and its
document is served at ``{did_url}/ids/idn_…/did.json``. The identity domain
(almena.id) is kept apart from the API's origin because a did:web cannot move:
it proxies ``/ids/`` and ``/.well-known/`` to this API.

The root tenant's identity is the exception: it is the identity domain's own
DID, ``did:web:{host}``, served at ``{did_url}/.well-known/did.json``.

Its DIDComm service depends on what acts as it: a mediator's names the address
the mediator listens on; the tenant's, an issuer's or a verifier's names the
DID of the mediator it picked, so messages to it are routed there.

An issuer's, a verifier's or a mediator's document names its tenant's DID as
its `controller`: the chain root → tenant → components, written in the
documents. A tenant with a certification in force names its domain, proved by
DNS, as a `LinkedDomains` service. Verification methods arrive when members
sign from their wallets.

An issuer, verifier or mediator that is still a draft (unpublished) has no
public document, and no document routes messages through a draft mediator.
"""

import uuid
from typing import Any
from urllib.parse import quote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api.models import Certification, Identity, Issuer, Mediator, Tenant, Verifier

# The path segment identities live under, in the DID and in the URL.
PATH = "ids"


def domain_did(did_url: str) -> str:
    """The identity domain's own DID: the root tenant's."""
    parts = urlsplit(did_url)
    # did:web writes the port percent-encoded: `localhost%3A8000`.
    host = quote(parts.netloc, safe="")
    path = [quote(p, safe="") for p in parts.path.split("/") if p]
    return ":".join(["did:web", host, *path])


def did_for(did_url: str, slug: str) -> str:
    return ":".join([domain_did(did_url), PATH, slug])


def url_for(did_url: str, slug: str) -> str:
    """Where `did_for(did_url, slug)` resolves."""
    return f"{did_url.rstrip('/')}/{PATH}/{slug}/did.json"


def domain_url(did_url: str) -> str:
    """Where `domain_did(did_url)` resolves."""
    return f"{did_url.rstrip('/')}/.well-known/did.json"


async def is_root_identity(db: AsyncSession, identity_id: uuid.UUID) -> bool:
    found = await db.scalar(select(Tenant.id).where(Tenant.root, Tenant.identity_id == identity_id))
    return found is not None


DID_CONTEXT = "https://www.w3.org/ns/did/v1"
# DIF Well Known DID Configuration, which defines the LinkedDomains service.
LINKED_DOMAINS_CONTEXT = "https://identity.foundation/.well-known/did-configuration/v1"


def document(
    did: str,
    endpoint: str | None,
    controller: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """The DID document an identity publishes: `endpoint` is where its DIDComm
    messages go (an address, or a mediator's DID), `controller` the DID of the
    tenant it belongs to, `domain` the tenant's certified domain; each if any."""
    context = [DID_CONTEXT, LINKED_DOMAINS_CONTEXT] if domain else [DID_CONTEXT]
    doc: dict[str, Any] = {"@context": context, "id": did}
    if controller:
        doc["controller"] = controller
    service: list[dict[str, Any]] = []
    if endpoint:
        service.append(
            {
                "id": f"{did}#didcomm",
                "type": "DIDCommMessaging",
                "serviceEndpoint": {"uri": endpoint, "accept": ["didcomm/v2"]},
            }
        )
    if domain:
        service.append(
            {
                "id": f"{did}#linked-domain",
                "type": "LinkedDomains",
                "serviceEndpoint": f"https://{domain}",
            }
        )
    if service:
        doc["service"] = service
    return doc


async def did_of(db: AsyncSession, did_url: str, identity: Identity) -> str:
    """An identity's DID: the domain's own for the root's, else under /ids/."""
    if await is_root_identity(db, identity.id):
        return domain_did(did_url)
    return did_for(did_url, identity.slug)


async def controller_for(db: AsyncSession, did_url: str, identity_id: uuid.UUID) -> str | None:
    """The DID of the tenant an issuer's, verifier's or mediator's identity
    belongs to; none for the tenant's own identity."""
    for model in (Issuer, Verifier, Mediator):
        tenant_id = await db.scalar(select(model.tenant_id).where(model.identity_id == identity_id))
        if tenant_id is not None:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None or tenant.identity_id is None:
                return None
            owner = await db.get(Identity, tenant.identity_id)
            return None if owner is None else await did_of(db, did_url, owner)
    return None


async def linked_domain(db: AsyncSession, identity_id: uuid.UUID) -> str | None:
    """The certified domain of the tenant whose own identity this is."""
    domain: str | None = await db.scalar(
        select(Certification.domain)
        .join(Tenant, Tenant.id == Certification.tenant_id)
        .where(Tenant.identity_id == identity_id, Certification.status == "approved")
    )
    return domain


async def document_for(db: AsyncSession, did_url: str, identity: Identity) -> dict[str, Any]:
    """The DID document `identity` publishes."""
    return document(
        await did_of(db, did_url, identity),
        await endpoint_for(db, did_url, identity.id),
        controller=await controller_for(db, did_url, identity.id),
        domain=await linked_domain(db, identity.id),
    )


async def endpoint_for(db: AsyncSession, did_url: str, identity_id: uuid.UUID) -> str | None:
    """Where the DIDComm messages of an identity go, from what acts as it."""
    url = await db.scalar(select(Mediator.url).where(Mediator.identity_id == identity_id))
    if url is not None:
        return url
    # The tenant, issuer or verifier acting as it: at most one of them.
    for model in (Tenant, Issuer, Verifier):
        mediator = await db.scalar(
            select(Mediator)
            .join(model, model.mediator_id == Mediator.id)
            .where(model.identity_id == identity_id)
        )
        # A draft mediator's DID does not resolve: nothing to route through yet.
        if mediator is not None and mediator.published_at is not None:
            return did_for(did_url, mediator.identity.slug)
    return None


async def is_draft(db: AsyncSession, identity_id: uuid.UUID) -> bool:
    """Whether the issuer, verifier or mediator acting as it is unpublished:
    then its DID does not resolve. The tenant's own identity always does."""
    for model in (Issuer, Verifier, Mediator):
        found = await db.scalar(
            select(model.id).where(model.identity_id == identity_id, model.published_at.is_(None))
        )
        if found is not None:
            return True
    return False
