"""A vault in the process's memory: what the tests use, and a reference provider."""

from registry_api.vault.base import Vault, check_path


class MemoryVault(Vault):
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}

    async def read(self, path: str) -> dict[str, str] | None:
        secret = self.secrets.get(check_path(path))
        return dict(secret) if secret is not None else None

    async def write(self, path: str, data: dict[str, str]) -> None:
        self.secrets[check_path(path)] = dict(data)

    async def delete(self, path: str) -> None:
        self.secrets.pop(check_path(path), None)

    async def list(self, prefix: str) -> list[str]:
        base = check_path(prefix, prefix=True) + "/"
        names = set()
        for path in self.secrets:
            if path.startswith(base):
                head, slash, _ = path[len(base) :].partition("/")
                names.add(head + slash)
        return sorted(names)

    async def ping(self) -> bool:
        return True
