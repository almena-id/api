"""The address a mediator listens on, as its DID document publishes it.

A mediator is registered, not discovered: nothing is fetched from the address
typed, it is only checked to be one a wallet can reach securely. A tenant's
listens on a subdomain of one of its verified domains (`address`), when
registered and when moved; only the root's, set at install, is typed whole
(`endpoint`).
"""

import ipaddress
import re
from urllib.parse import quote, urlsplit, urlunsplit

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


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


URL_MAX = 2048


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
    cleaned = urlunsplit((scheme, parts.netloc.lower(), path, "", ""))
    # As kept, `https://` included: the column holds 2048.
    if len(cleaned) > URL_MAX:
        raise MediatorError("mediator_invalid")
    return cleaned


def address(subdomain: str, domain: str) -> str:
    """`https://{subdomain}.{domain}`: the subdomain typed (one label or more,
    `eu.relay`), lowercased, IDNs in their ASCII form; `domain` is one of the
    tenant's, already clean."""
    typed = subdomain.strip().lower().strip(".")
    try:
        typed = typed.encode("idna").decode("ascii")
    except UnicodeError:
        raise MediatorError("subdomain_invalid") from None
    host = f"{typed}.{domain}"
    if not typed or len(host) > 253 or not all(_LABEL.match(part) for part in typed.split(".")):
        raise MediatorError("subdomain_invalid")
    return f"https://{host}"


def did_web(url: str) -> str:
    """The `did:web` a mediator at `url` (as `endpoint` keeps it) answers as:
    the one its own DID document has, with the keys a `forward` to it is
    encrypted to. `https://mediator.almena.id` → `did:web:mediator.almena.id`;
    a port is written `%3A`, path segments `:`-separated (did:web 1.0)."""
    parts = urlsplit(url)
    segments = [quote(part, safe="") for part in parts.path.split("/") if part]
    return ":".join(["did:web", parts.netloc.replace(":", "%3A"), *segments])
