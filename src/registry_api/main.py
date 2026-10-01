"""FastAPI application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scalar_fastapi import add_scalar_reference

from registry_api import __version__
from registry_api.api.router import api_router
from registry_api.api.routes import did_documents, health, well_known
from registry_api.config import get_settings
from registry_api.db import get_engine
from registry_api.logs import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="Almena Registry API",
        version=__version__,
        lifespan=lifespan,
        # The reference is Scalar's (below), not Swagger UI or ReDoc.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        servers=[
            {"url": settings.public_url, "description": "Public"},
            {"url": "/", "description": "This server"},
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Outermost, so every request, preflights and errors included, gets its id.
    app.add_middleware(RequestContextMiddleware)
    if settings.docs_enabled:
        add_scalar_reference(app, route="/docs")
    app.include_router(health.router)
    app.include_router(did_documents.router)
    app.include_router(well_known.router)
    app.include_router(api_router)
    return app


app = create_app()
