"""What the API asks of a vault, whichever service is behind it.

A vault keeps secrets — private keys, service credentials — as small maps of
strings under a path such as ``tenants/{tenant_id}/issuers/{issuer_id}/messaging``.
Paths are portable: lowercase letters, digits and hyphens in segments joined by
``/``, which every provider can map to its own names (Azure Key Vault, for one,
takes neither ``/`` nor ``_``). Build them with `registry_api.vault.paths`.

A provider implements the abstract methods below; nothing outside this package
knows which one is configured.
"""

import re
from abc import ABC, abstractmethod

_SEGMENT = re.compile(r"[a-z0-9][a-z0-9-]*")


class VaultError(Exception):
    """The vault could not be reached, or refused what was asked."""


def check_path(path: str, *, prefix: bool = False) -> str:
    """The path itself, or ValueError. A prefix (to list or delete under) may end in ``/``."""
    body = path.removesuffix("/") if prefix else path
    if not body or not all(_SEGMENT.fullmatch(segment) for segment in body.split("/")):
        raise ValueError(f"not a vault path: {path!r}")
    return body


class Vault(ABC):
    """A secret store. Every method raises VaultError when the service fails."""

    @abstractmethod
    async def read(self, path: str) -> dict[str, str] | None:
        """The latest version of the secret, or None when there is none."""

    @abstractmethod
    async def write(self, path: str, data: dict[str, str]) -> None:
        """Store the secret, as a new version when there was one."""

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete the secret with every version it had; nothing when there is none."""

    @abstractmethod
    async def list(self, prefix: str) -> list[str]:
        """The names right under the prefix, sorted: a secret as is, a branch ending in ``/``."""

    @abstractmethod
    async def ping(self) -> bool:
        """Whether the vault answers and lets the API in (for readiness)."""

    async def aclose(self) -> None:
        """Release connections; the app's shutdown calls it."""
        return None

    async def delete_tree(self, prefix: str) -> None:
        """Delete every secret under the prefix (a tenant's, when it goes)."""
        base = check_path(prefix, prefix=True)
        for name in await self.list(base):
            if name.endswith("/"):
                await self.delete_tree(f"{base}/{name}")
            else:
                await self.delete(f"{base}/{name}")
