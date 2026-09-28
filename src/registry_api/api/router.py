"""Versioned API router: mount new route modules here."""

from fastapi import APIRouter

from registry_api.api.routes import (
    auth,
    certification,
    directory,
    members,
    publication,
    review,
    signing,
    tenants,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(directory.router)
api_router.include_router(members.router)
api_router.include_router(publication.router)
api_router.include_router(publication.catalog)
api_router.include_router(signing.router)
api_router.include_router(certification.router)
api_router.include_router(certification.public)
api_router.include_router(review.router)
