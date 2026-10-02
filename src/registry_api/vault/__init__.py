"""The vault where the platform keeps its secrets, behind a provider of choice.

`Vault` (in base.py) is the interface; each provider is a module of its own:
OpenBao today (openbao.py). Another service — Azure Key Vault, AWS Secrets
Manager — is one more module implementing `Vault`, a value for
``REGISTRY_VAULT_PROVIDER`` and a branch in `_build`; nothing else changes.
"""

from functools import lru_cache

from registry_api.config import Settings, get_settings
from registry_api.vault.base import Vault, VaultError

__all__ = ["Vault", "VaultError", "close_vault", "get_vault"]


def _build(settings: Settings) -> Vault:
    match settings.vault_provider:
        case "openbao":
            from registry_api.vault.openbao import OpenBaoVault

            return OpenBaoVault(
                settings.openbao_addr,
                settings.openbao_mount,
                settings.openbao_role_id,
                settings.openbao_secret_id.get_secret_value(),
            )


@lru_cache
def _vault() -> Vault:
    return _build(get_settings())


def get_vault() -> Vault:
    """FastAPI dependency: one vault per process; tests override it."""
    return _vault()


async def close_vault() -> None:
    if _vault.cache_info().currsize:
        await _vault().aclose()
        _vault.cache_clear()
