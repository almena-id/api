"""Recognising an Almena mediator from its public origin.

A mediator's DID is the `did:web` of its origin, and the document at
``/.well-known/did.json`` names a ``DIDCommMessaging`` service: that is what
makes an origin a mediator rather than just any website.
"""

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import httpx


class MediatorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _local(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin(url: str) -> str:
    """`https://host[:port]`, from whatever was typed; plain HTTP only on loopback."""
    parts = urlsplit(url.strip() if "://" in url else f"https://{url.strip()}")
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host:
        raise MediatorError("mediator_invalid")
    if parts.scheme == "http" and not _local(host):
        raise MediatorError("mediator_insecure")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


async def resolve(http: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """The mediator's origin and DID, or `MediatorError` if it is not one."""
    base = origin(url)
    try:
        response = await http.get(f"{base}/.well-known/did.json", follow_redirects=False)
    except httpx.HTTPError:
        raise MediatorError("mediator_unreachable") from None
    # A site that answers, but not with a DID document, is not a mediator;
    # only one that does not answer (or fails) is unreachable.
    if response.status_code >= 500:
        raise MediatorError("mediator_unreachable")
    try:
        document: Any = response.json() if response.status_code == 200 else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise MediatorError("mediator_not_a_mediator")
    did = document.get("id")
    services = document.get("service")
    if not isinstance(did, str) or not did.startswith("did:web:"):
        raise MediatorError("mediator_not_a_mediator")
    if not isinstance(services, list) or not any(
        isinstance(s, dict) and s.get("type") == "DIDCommMessaging" for s in services
    ):
        raise MediatorError("mediator_not_a_mediator")
    return base, did
