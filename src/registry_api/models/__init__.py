"""ORM models. Import every model module here so Alembic sees its tables."""

from registry_api.models.almena import WalletRequest
from registry_api.models.application import APPLICATION_STATUSES, Application, ApplicationFile
from registry_api.models.base import Base
from registry_api.models.custom_field import CustomField
from registry_api.models.domain import TenantDomain
from registry_api.models.form import Form
from registry_api.models.identity import DidLogEntry, Identity
from registry_api.models.issuer import Issuer
from registry_api.models.login_code import LoginCode
from registry_api.models.mediator import Mediator
from registry_api.models.oauth import AccountMove, OAuthFlow, UserIdentity
from registry_api.models.session import Session
from registry_api.models.status_list import StatusList
from registry_api.models.tenant import ROLES, Role, Tenant, TenantInvitation, TenantMember
from registry_api.models.user import User
from registry_api.models.verification import Verification
from registry_api.models.verifier import Verifier

__all__ = [
    "APPLICATION_STATUSES",
    "ROLES",
    "AccountMove",
    "Application",
    "ApplicationFile",
    "Base",
    "CustomField",
    "DidLogEntry",
    "Form",
    "Identity",
    "Issuer",
    "LoginCode",
    "Mediator",
    "OAuthFlow",
    "Role",
    "Session",
    "StatusList",
    "Tenant",
    "TenantDomain",
    "TenantInvitation",
    "TenantMember",
    "User",
    "UserIdentity",
    "Verification",
    "Verifier",
    "WalletRequest",
]
