"""Versioned API router: mount new route modules here."""

from fastapi import APIRouter

from registry_api.api.routes import auth

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
