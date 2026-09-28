"""The address a mediator listens on, as its DID document publishes it.

A mediator is registered, not discovered: nothing is fetched from the address
typed, it is only checked to be one a wallet can reach securely.
"""

import ipaddress
from urllib.parse import urlsplit, urlunsplit


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


def endpoint(url: str) -> str:
    """The address, cleaned: `https://` added when no scheme is typed; plain
    HTTP only on loopback; no credentials, query or fragment."""
    typed = url.strip()
    try:
        parts = urlsplit(typed if "://" in typed else f"https://{typed}")
        host = parts.hostname
        parts.port  # noqa: B018 — raises on a malformed port
    except ValueError:
        raise MediatorError("mediator_invalid") from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise MediatorError("mediator_invalid")
    if parts.query or parts.fragment:
        raise MediatorError("mediator_invalid")
    if scheme == "http" and not _local(host):
        raise MediatorError("mediator_insecure")
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, parts.netloc.lower(), path, "", ""))
