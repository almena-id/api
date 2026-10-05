"""The address a mediator listens on, as its DID document publishes it.

A mediator is registered, not discovered: nothing is fetched from the address
typed, it is only checked to be one a wallet can reach securely. A mediator
listens on a subdomain of one of its tenant's verified domains (`address`),
when registered and when moved.
"""

import re
from urllib.parse import quote, urlsplit

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class MediatorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    """The `did:web` a mediator at `url` (as `address` makes it) answers as:
    the one its own DID document has, with the keys a `forward` to it is
    encrypted to. `https://mediator.almena.id` → `did:web:mediator.almena.id`;
    a port is written `%3A`, path segments `:`-separated (did:web 1.0)."""
    parts = urlsplit(url)
    segments = [quote(part, safe="") for part in parts.path.split("/") if part]
    return ":".join(["did:web", parts.netloc.replace(":", "%3A"), *segments])
