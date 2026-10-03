"""Versioned API router: mount new route modules here."""

from fastapi import APIRouter

from registry_api.api.routes import (
    account,
    applications,
    auth,
    custom_fields,
    directory,
    domains,
    fields,
    forms,
    issuance,
    issuer_credentials,
    members,
    publication,
    queues,
    signing,
    status_lists,
    tenants,
    tokens,
    verifications,
    wallet,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(account.router)
api_router.include_router(tokens.router)
api_router.include_router(wallet.router)
api_router.include_router(wallet.signing)
api_router.include_router(tenants.router)
api_router.include_router(domains.router)
api_router.include_router(forms.router)
api_router.include_router(fields.api)
api_router.include_router(custom_fields.router)
api_router.include_router(issuer_credentials.router)
api_router.include_router(applications.router)
api_router.include_router(applications.offers)
api_router.include_router(applications.inbox)
api_router.include_router(issuance.router)
api_router.include_router(status_lists.router)
api_router.include_router(status_lists.credentials)
api_router.include_router(directory.router)
api_router.include_router(members.router)
api_router.include_router(publication.router)
api_router.include_router(publication.catalog)
api_router.include_router(signing.router)
api_router.include_router(queues.router)
api_router.include_router(verifications.router)
api_router.include_router(verifications.public)
