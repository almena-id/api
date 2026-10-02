import json
import uuid

import httpx
import pytest

from registry_api.vault import VaultError, paths
from registry_api.vault.base import Vault
from registry_api.vault.memory import MemoryVault
from registry_api.vault.openbao import OpenBaoVault

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
ISSUER = uuid.UUID("22222222-2222-4222-8222-222222222222")


class FakeOpenBao:
    """OpenBao's AppRole sign-in, health and a KV v2 mount, `almena`, in memory."""

    def __init__(self) -> None:
        self.secrets: dict[str, list[dict[str, str]]] = {}
        self.tokens: set[str] = set()
        self.logins = 0
        self.sealed = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sys/health":
            return httpx.Response(503 if self.sealed else 200, json={})
        if path == "/v1/auth/approle/login":
            body = json.loads(request.content)
            if body != {"role_id": "registry-api", "secret_id": "s3cret"}:
                return httpx.Response(400, json={"errors": ["invalid secret id"]})
            self.logins += 1
            token = f"token-{self.logins}"
            self.tokens.add(token)
            return httpx.Response(
                200, json={"auth": {"client_token": token, "lease_duration": 3600}}
            )
        if request.headers.get("X-Vault-Token") not in self.tokens:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        kind, _, key = path.removeprefix("/v1/almena/").partition("/")
        match request.method, kind:
            case "GET", "data":
                if key not in self.secrets:
                    return httpx.Response(404, json={"errors": []})
                return httpx.Response(200, json={"data": {"data": self.secrets[key][-1]}})
            case "POST", "data":
                self.secrets.setdefault(key, []).append(json.loads(request.content)["data"])
                return httpx.Response(200, json={"data": {"version": len(self.secrets[key])}})
            case "DELETE", "metadata":
                self.secrets.pop(key, None)
                return httpx.Response(204)
            case "LIST", "metadata":
                names = {
                    head + slash
                    for name in self.secrets
                    if name.startswith(key + "/")
                    for head, slash, _ in [name[len(key) + 1 :].partition("/")]
                }
                if not names:
                    return httpx.Response(404, json={"errors": []})
                return httpx.Response(200, json={"data": {"keys": sorted(names)}})
        return httpx.Response(405)


@pytest.fixture
def bao() -> FakeOpenBao:
    return FakeOpenBao()


def _openbao(bao: FakeOpenBao, secret_id: str = "s3cret") -> OpenBaoVault:
    return OpenBaoVault(
        "http://openbao.test",
        "almena",
        "registry-api",
        secret_id,
        transport=httpx.MockTransport(bao.handle),
    )


@pytest.fixture(params=["memory", "openbao"])
def store(request: pytest.FixtureRequest, bao: FakeOpenBao) -> Vault:
    """Every provider must behave the same."""
    return MemoryVault() if request.param == "memory" else _openbao(bao)


def test_paths_hang_from_the_tenant() -> None:
    assert paths.tenant(TENANT) == f"tenants/{TENANT}"
    assert (
        paths.entity(TENANT, "issuers", ISSUER, "messaging")
        == f"tenants/{TENANT}/issuers/{ISSUER}/messaging"
    )


@pytest.mark.parametrize("name", ["", "Messaging", "a_b", "../x", "a//b"])
def test_paths_are_portable(name: str) -> None:
    with pytest.raises(ValueError):
        paths.entity(TENANT, "issuers", ISSUER, name)


async def test_write_read_and_delete(store: Vault) -> None:
    path = paths.entity(TENANT, "issuers", ISSUER, "messaging")
    assert await store.read(path) is None
    await store.write(path, {"kty": "OKP", "d": "one"})
    await store.write(path, {"kty": "OKP", "d": "two"})
    assert await store.read(path) == {"kty": "OKP", "d": "two"}
    await store.delete(path)
    assert await store.read(path) is None
    await store.delete(path)  # Nothing to delete: no error.


async def test_list_and_delete_a_tenant(store: Vault) -> None:
    other = uuid.uuid4()
    await store.write(paths.entity(TENANT, "issuers", ISSUER, "messaging"), {"d": "1"})
    await store.write(paths.entity(TENANT, "verifiers", ISSUER, "messaging"), {"d": "2"})
    await store.write(paths.entity(other, "issuers", ISSUER, "messaging"), {"d": "3"})
    assert await store.list(paths.tenant(TENANT)) == ["issuers/", "verifiers/"]
    assert await store.list(f"tenants/{TENANT}/issuers/{ISSUER}") == ["messaging"]
    await store.delete_tree(paths.tenant(TENANT))
    assert await store.list(paths.tenant(TENANT)) == []
    assert await store.read(paths.entity(other, "issuers", ISSUER, "messaging")) == {"d": "3"}


async def test_rejects_paths_that_are_not_portable(store: Vault) -> None:
    with pytest.raises(ValueError):
        await store.read("tenants/../root")


async def test_openbao_signs_in_once_and_again_when_refused(bao: FakeOpenBao) -> None:
    vault = _openbao(bao)
    await vault.write("tenants/a/b", {"d": "1"})
    await vault.read("tenants/a/b")
    assert bao.logins == 1
    bao.tokens.clear()  # Revoked.
    assert await vault.read("tenants/a/b") == {"d": "1"}
    assert bao.logins == 2


async def test_openbao_with_a_wrong_secret_id(bao: FakeOpenBao) -> None:
    vault = _openbao(bao, secret_id="wrong")
    with pytest.raises(VaultError):
        await vault.read("tenants/a/b")
    assert not await vault.ping()


async def test_openbao_ping(bao: FakeOpenBao) -> None:
    vault = _openbao(bao)
    assert await vault.ping()
    bao.sealed = True
    assert not await vault.ping()


async def test_openbao_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    vault = OpenBaoVault(
        "http://openbao.test", "almena", "registry-api", "s", transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(VaultError):
        await vault.read("tenants/a/b")
    assert not await vault.ping()
