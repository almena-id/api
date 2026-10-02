"""OpenBao (or HashiCorp Vault, which speaks the same API): a KV v2 mount.

The API signs in with an AppRole (`task openbao:setup` provisions it) and
keeps the token until shortly before it expires, or until OpenBao refuses it,
then signs in again.
"""

import asyncio
import time
from typing import Any

import httpx

from registry_api.vault.base import Vault, VaultError, check_path

# Sign in again once this share of a token's lifetime has gone by.
_RENEW_AT = 0.8


class OpenBaoVault(Vault):
    def __init__(
        self,
        addr: str,
        mount: str,
        role_id: str,
        secret_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=addr.rstrip("/"), timeout=5.0, transport=transport)
        self._mount = mount.strip("/")
        self._role_id = role_id
        self._secret_id = secret_id
        self._token: str | None = None
        self._renew_after = 0.0
        self._lock = asyncio.Lock()

    async def _login(self) -> str:
        try:
            response = await self._http.post(
                "/v1/auth/approle/login",
                json={"role_id": self._role_id, "secret_id": self._secret_id},
            )
        except httpx.HTTPError as error:
            raise VaultError(f"OpenBao unreachable: {type(error).__name__}") from error
        if response.status_code != 200:
            raise VaultError(f"OpenBao refused the sign-in: {response.status_code}")
        auth = response.json()["auth"]
        self._token = str(auth["client_token"])
        self._renew_after = time.monotonic() + _RENEW_AT * float(auth["lease_duration"])
        return self._token

    async def _current_token(self, *, refresh: bool = False) -> str:
        async with self._lock:
            if refresh or self._token is None or time.monotonic() >= self._renew_after:
                return await self._login()
            return self._token

    async def _request(
        self, method: str, url: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        token = await self._current_token()
        try:
            response = await self._http.request(
                method, url, json=json, headers={"X-Vault-Token": token}
            )
            if response.status_code == 403:
                # Revoked or expired early: one more try with a fresh token.
                token = await self._current_token(refresh=True)
                response = await self._http.request(
                    method, url, json=json, headers={"X-Vault-Token": token}
                )
        except httpx.HTTPError as error:
            raise VaultError(f"OpenBao unreachable: {type(error).__name__}") from error
        if response.status_code >= 400 and response.status_code != 404:
            raise VaultError(f"OpenBao answered {response.status_code} to {method} {url}")
        return response

    async def read(self, path: str) -> dict[str, str] | None:
        response = await self._request("GET", f"/v1/{self._mount}/data/{check_path(path)}")
        if response.status_code == 404:
            return None
        data: dict[str, str] = response.json()["data"]["data"]
        return data

    async def write(self, path: str, data: dict[str, str]) -> None:
        await self._request(
            "POST", f"/v1/{self._mount}/data/{check_path(path)}", json={"data": data}
        )

    async def delete(self, path: str) -> None:
        # The metadata goes, and with it every version.
        await self._request("DELETE", f"/v1/{self._mount}/metadata/{check_path(path)}")

    async def list(self, prefix: str) -> list[str]:
        base = check_path(prefix, prefix=True)
        response = await self._request("LIST", f"/v1/{self._mount}/metadata/{base}")
        if response.status_code == 404:
            return []
        return sorted(response.json()["data"]["keys"])

    async def ping(self) -> bool:
        try:
            health = await self._http.get("/v1/sys/health")
            # 200 only when initialized, unsealed and active.
            if health.status_code != 200:
                return False
            await self._current_token()
        except (httpx.HTTPError, VaultError):
            return False
        return True

    async def aclose(self) -> None:
        await self._http.aclose()
