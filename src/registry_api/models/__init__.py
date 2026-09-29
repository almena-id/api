"""ORM models. Import every model module here so Alembic sees its tables."""

from registry_api.models.almena import WalletRequest
from registry_api.models.base import Base
from registry_api.models.domain import TenantDomain
from registry_api.models.identity import DidLogEntry, Identity
from registry_api.models.issuer import Issuer
from registry_api.models.login_code import LoginCode
from registry_api.models.mediator import Mediator
from registry_api.models.oauth import AccountMove, OAuthFlow, UserIdentity
from registry_api.models.session import Session
from registry_api.models.tenant import ROLES, Role, Tenant, TenantInvitation, TenantMember
from registry_api.models.user import User
from registry_api.models.verifier import Verifier

__all__ = [
    "ROLES",
    "AccountMove",
    "Base",
    "DidLogEntry",
    "Identity",
    "Issuer",
    "LoginCode",
    "Mediator",
    "OAuthFlow",
    "Role",
    "Session",
    "Tenant",
    "TenantDomain",
    "TenantInvitation",
    "TenantMember",
    "User",
    "UserIdentity",
    "Verifier",
    "WalletRequest",
]
