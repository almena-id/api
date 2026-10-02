"""The DIDs of the tenants' identities: `did:webvh` under the identity domain.

An identity with slug ``idn_…`` is ``did:webvh:{SCID}:{host}:ids:idn_…``; its
log is served at ``{did_url}/ids/idn_…/did.jsonl`` and, for resolvers that
know only did:web, its current document at ``…/did.json`` as
``did:web:{host}:ids:idn_…``. The identity domain (almena.id) is kept apart
from the API's origin because the DID names it: it proxies ``/ids/`` and
``/.well-known/`` to this API. The root tenant's identity is the domain's own:
``did:webvh:{SCID}:{host}``, at ``{did_url}/.well-known/did.jsonl``.

**The registry never signs.** It works out the document an identity should
publish (`desired`) and the update keys that may sign it — those the tenant's
signing flow names (`signing_flows`) — and prepares the log entry that would
take the log there; one of them signs it from a wallet, and only then is it
published. Until its first
entry is signed an identity is *pending* and has no DID; when what it should
publish differs from what it last signed it is *outdated*.

Its DIDComm service depends on what acts as it: a mediator's names the address
the mediator listens on; the tenant's, an issuer's or a verifier's names the
DID of the mediator it picked, so messages to it are routed there. An
issuer's, a verifier's or a mediator's document names its tenant's DID as its
`controller`. A tenant names the domains it has proved by DNS, those linked to
it, as a `LinkedDomains` service.

**Keys.** What an identity signs is checked against the keys its document
lists under `assertionMethod`, each one embedded as a `Multikey` verification
method (`{did}#{multikey}`) so a verifier needs to resolve nothing else: for a
tenant's own identity, the keys its signing flow names (under `authentication` too,
for the presentation that is its `whois.vp`; an issuer's, verifier's or
mediator's `authentication` names those same keys as methods of the tenant's
DID, its controller, which presents the tenant's endorsement of it) (the same keys that update
its log); for an
issuer or a verifier, the wallets of the member its signing system names; a
mediator signs nothing. Every key is a person's registry `did:key`: the
registry holds none.

**Messaging.** An issuer or a verifier gets a key to receive messages
(X25519, under `keyAgreement`) when its signer signs it: see `messaging_keys`.

**Phases.** A document says only what is true already, so it grows with the
item: its DID once the first entry is signed (until then, the template);
`controller` and `authentication` once the tenant's own DID is signed;
`assertionMethod` once its signer has a wallet; `keyAgreement` once its
messaging key exists; the DIDComm service once there is somewhere to deliver
to — a published mediator and, for an issuer or verifier, a key to encrypt to.

An issuer, verifier or mediator that is still a draft (unpublished) has no
public document, and no document routes messages through a draft mediator.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import messaging_keys, signing_flows, webvh
from registry_api.models import (
    DidLogEntry,
    Identity,
    Issuer,
    Mediator,
    Tenant,
    TenantDomain,
    TenantMember,
    UserIdentity,
    Verifier,
)

# The path segment identities live under, in the DID and in the URL.
PATH = "ids"

Status = Literal["pending", "signed", "outdated"]


def _host_and_path(did_url: str) -> tuple[str, list[str]]:
    parts = urlsplit(did_url)
    # The DID writes the port percent-encoded: `localhost%3A8000`.
    host = quote(parts.netloc, safe="")
    return host, [quote(p, safe="") for p in parts.path.split("/") if p]


def template_for(did_url: str, slug: str, root: bool) -> str:
    """The identity's DID with `{SCID}` still in place of the SCID."""
    host, path = _host_and_path(did_url)
    return webvh.did_prefix(host, path if root else [*path, PATH, slug])


def base_url(did_url: str, slug: str, root: bool) -> str:
    """Where the identity's `did.jsonl` and `did.json` are served."""
    base = did_url.rstrip("/")
    return f"{base}/.well-known" if root else f"{base}/{PATH}/{slug}"


async def is_root_identity(db: AsyncSession, identity_id: uuid.UUID) -> bool:
    found = await db.scalar(select(Tenant.id).where(Tenant.root, Tenant.identity_id == identity_id))
    return found is not None


DID_CONTEXT = "https://www.w3.org/ns/did/v1"
# Multikey verification methods.
MULTIKEY_CONTEXT = "https://w3id.org/security/multikey/v1"
# DIF Well Known DID Configuration, which defines the LinkedDomains service.
LINKED_DOMAINS_CONTEXT = "https://identity.foundation/.well-known/did-configuration/v1"


def document(
    did: str,
    endpoint: str | None,
    controller: str | None = None,
    domains: list[str] | None = None,
    keys: list[str] | None = None,
    authenticate: bool = False,
    controller_keys: list[str] | None = None,
    agreement_key: str | None = None,
) -> dict[str, Any]:
    """The DID document an identity publishes: `endpoint` is where its DIDComm
    messages go (an address, or a mediator's DID), `controller` the DID of the
    tenant it belongs to, `domains` the tenant's proved domains, `keys` the
    multikeys that sign for it, `agreement_key` the one messages to it are
    encrypted to; each if any."""
    context = [DID_CONTEXT]
    if keys or agreement_key:
        context.append(MULTIKEY_CONTEXT)
    if domains:
        context.append(LINKED_DOMAINS_CONTEXT)
    doc: dict[str, Any] = {"@context": context, "id": did}
    if controller:
        doc["controller"] = controller
    methods = [*(keys or []), *([agreement_key] if agreement_key else [])]
    if methods:
        doc["verificationMethod"] = [
            {
                "id": f"{did}#{key}",
                "type": "Multikey",
                "controller": did,
                "publicKeyMultibase": key,
            }
            for key in methods
        ]
    if keys:
        doc["assertionMethod"] = [f"{did}#{key}" for key in keys]
        # A tenant's keys also sign its presentations (its `whois.vp`).
        if authenticate:
            doc["authentication"] = [f"{did}#{key}" for key in keys]
    # A component's presentations are signed by its controller, the tenant:
    # the keys its signing flow names, as methods of the tenant's DID.
    if controller and controller_keys:
        doc["authentication"] = [f"{controller}#{key}" for key in controller_keys]
    if agreement_key:
        doc["keyAgreement"] = [f"{did}#{agreement_key}"]
    service: list[dict[str, Any]] = []
    if endpoint:
        service.append(
            {
                "id": f"{did}#didcomm",
                "type": "DIDCommMessaging",
                "serviceEndpoint": {"uri": endpoint, "accept": ["didcomm/v2"]},
            }
        )
    if domains:
        origins = [f"https://{domain}" for domain in domains]
        service.append(
            {
                "id": f"{did}#linked-domain",
                "type": "LinkedDomains",
                # One origin as a string, several as `origins` (DIF Well Known
                # DID Configuration allows both).
                "serviceEndpoint": origins[0] if len(origins) == 1 else {"origins": origins},
            }
        )
    if service:
        doc["service"] = service
    return doc


async def controller_for(db: AsyncSession, identity_id: uuid.UUID) -> str | None:
    """The DID of the tenant an issuer's, verifier's or mediator's identity
    belongs to; none for the tenant's own identity, or while it is pending."""
    for model in (Issuer, Verifier, Mediator):
        tenant_id = await db.scalar(select(model.tenant_id).where(model.identity_id == identity_id))
        if tenant_id is not None:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None or tenant.identity_id is None:
                return None
            owner = await db.get(Identity, tenant.identity_id)
            return None if owner is None else owner.did
    return None


async def linked_domains(db: AsyncSession, identity_id: uuid.UUID) -> list[str]:
    """The verified domains linked to the tenant whose own identity this is."""
    tenant_id = await db.scalar(select(Tenant.id).where(Tenant.identity_id == identity_id))
    if tenant_id is None:
        return []
    linked = await db.scalars(
        select(TenantDomain.domain)
        .where(TenantDomain.tenant_id == tenant_id, TenantDomain.verified_at.is_not(None))
        .order_by(TenantDomain.created_at, TenantDomain.id)
    )
    return list(linked)


async def endpoint_for(db: AsyncSession, identity_id: uuid.UUID) -> str | None:
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
        # A draft or pending mediator's DID does not resolve: nothing to route through yet.
        if mediator is not None and mediator.published_at is not None:
            return mediator.identity.did
    return None


async def _wallets(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    subjects = await db.scalars(
        select(UserIdentity.subject).where(
            UserIdentity.user_id == user_id, UserIdentity.provider == "almena"
        )
    )
    return sorted({webvh.multikey(subject) for subject in subjects})


async def signing_keys(db: AsyncSession, identity: Identity) -> list[str]:
    """The multikeys that sign for the identity (see the module's **Keys**)."""
    tenant = await db.scalar(select(Tenant).where(Tenant.identity_id == identity.id))
    if tenant is not None:
        return await update_keys(db, tenant.id)
    item: Issuer | Verifier | None = await db.scalar(
        select(Issuer).where(Issuer.identity_id == identity.id)
    ) or await db.scalar(select(Verifier).where(Verifier.identity_id == identity.id))
    if item is None or item.signing != "single_user" or item.signer_id is None:
        return []
    # A signer who left the tenant signs nothing.
    member = await db.get(TenantMember, (item.tenant_id, item.signer_id))
    return [] if member is None else await _wallets(db, item.signer_id)


async def desired(db: AsyncSession, did_url: str, identity: Identity) -> dict[str, Any]:
    """The document the identity should publish now: under its DID, or its
    DID's template while it is pending."""
    root = await is_root_identity(db, identity.id)
    did = identity.did or template_for(did_url, identity.slug, root)
    own = await db.scalar(select(Tenant.id).where(Tenant.identity_id == identity.id))
    controller = await controller_for(db, identity.id)
    endpoint = await endpoint_for(db, identity.id)
    # An issuer or verifier with no key yet has nothing to be written to with.
    if identity.agreement_key is None and await messaging_keys.owner(db, identity.id):
        endpoint = None
    return document(
        did,
        endpoint,
        controller=controller,
        domains=await linked_domains(db, identity.id),
        keys=await signing_keys(db, identity),
        authenticate=own is not None,
        controller_keys=await update_keys(db, identity.tenant_id) if controller else None,
        agreement_key=identity.agreement_key,
    )


async def update_keys(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Who may sign the tenant's identities: what its signing flow says."""
    return await signing_flows.signers(db, tenant_id)


async def log(db: AsyncSession, identity_id: uuid.UUID) -> list[webvh.Entry]:
    """The identity's signed log, oldest first."""
    rows = await db.scalars(
        select(DidLogEntry.entry)
        .where(DidLogEntry.identity_id == identity_id)
        .order_by(DidLogEntry.version)
    )
    return [json.loads(row) for row in rows]


def _unsigned(entry: webvh.Entry) -> webvh.Entry:
    return {k: v for k, v in entry.items() if k != "proof"}


async def next_entry(db: AsyncSession, did_url: str, identity: Identity) -> webvh.Entry | None:
    """The entry that would bring the log to what the identity should publish;
    `None` when it is already there. `LogError("no_signers")` when nobody
    could sign it."""
    entries = await log(db, identity.id)
    state = await desired(db, did_url, identity)
    keys = await update_keys(db, identity.tenant_id)
    when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not entries:
        if not keys:
            raise webvh.LogError("no_signers")
        return webvh.genesis(str(state["id"]), state, keys, when)

    active = webvh.active_update_keys(entries)
    # With no admin wallet left the keys in force stay: a log cannot be left
    # with nobody able to sign it.
    parameters: dict[str, Any] = {"updateKeys": keys} if keys and keys != active else {}
    if not parameters and webvh.jcs(state) == webvh.jcs(entries[-1]["state"]):
        return None
    if not active:
        raise webvh.LogError("no_signers")
    return webvh.following(_unsigned(entries[-1]), parameters, state, when)


async def status_of(db: AsyncSession, did_url: str, identity: Identity) -> Status:
    if identity.did is None:
        return "pending"
    try:
        return "signed" if await next_entry(db, did_url, identity) is None else "outdated"
    except webvh.LogError:
        return "outdated"


def keys_under(document: dict[str, Any] | None, relationship: str) -> list[str]:
    """The multikeys a document lists under `relationship` (`assertionMethod`…)."""
    if not document:
        return []
    did = str(document["id"])
    return [
        str(ref).removeprefix(f"{did}#")
        for ref in document.get(relationship, [])
        if str(ref).startswith(f"{did}#")
    ]


async def published(db: AsyncSession, identity: Identity) -> dict[str, Any] | None:
    """The document the identity's log says now; `None` while pending."""
    entries = await log(db, identity.id)
    return entries[-1]["state"] if entries else None


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
