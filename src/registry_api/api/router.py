"""Versioned API router: mount new route modules here."""

from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")
