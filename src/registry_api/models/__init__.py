"""ORM models. Import every model module here so Alembic sees its tables."""

from registry_api.models.base import Base

__all__ = ["Base"]
