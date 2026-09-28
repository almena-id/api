"""ORM models. Import every model module here so Alembic sees its tables."""

from registry_api.models.base import Base
from registry_api.models.identity import Identity
from registry_api.models.issuer import Issuer
from registry_api.models.login_code import LoginCode
from registry_api.models.oauth import OAuthFlow, UserIdentity
from registry_api.models.session import Session
from registry_api.models.tenant import ROLES, Role, Tenant, TenantInvitation, TenantMember
from registry_api.models.user import User
from registry_api.models.verifier import Verifier

__all__ = [
    "ROLES",
    "Base",
    "Identity",
    "Issuer",
    "LoginCode",
    "OAuthFlow",
    "Role",
    "Session",
    "Tenant",
    "TenantInvitation",
    "TenantMember",
    "User",
    "UserIdentity",
    "Verifier",
]
