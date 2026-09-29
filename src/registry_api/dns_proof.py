"""Proving a domain with DNS: a TXT record the owner publishes.

A tenant links a domain (`api/routes/domains.py`) and gets the record that
proves it: ``_almena.{domain}`` saying ``almena-verify={token}``.
"""

import re
import secrets
from collections.abc import Awaitable, Callable

import dns.asyncresolver
import dns.exception
import dns.resolver

# Where the proof is published, and how it starts.
DNS_LABEL = "_almena"
DNS_PREFIX = "almena-verify="

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class DomainError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def domain(typed: str) -> str:
    """A bare domain from whatever was typed (`https://Acme.com/x` → `acme.com`)."""
    value = typed.strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise DomainError("domain_invalid") from None
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise DomainError("domain_invalid")
    if labels[-1].isdigit():
        raise DomainError("domain_invalid")
    return value


def new_token() -> str:
    return secrets.token_urlsafe(24)


def dns_record(domain: str, token: str) -> tuple[str, str]:
    """The TXT record that proves control of `domain`: its name and its value."""
    return f"{DNS_LABEL}.{domain}", f"{DNS_PREFIX}{token}"


TxtLookup = Callable[[str], Awaitable[list[str]]]


async def txt_records(name: str) -> list[str]:
    """The TXT records at `name`; none when it does not exist or does not answer."""
    try:
        answer = await dns.asyncresolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.Timeout:
        raise DomainError("dns_unavailable") from None
    return [b"".join(record.strings).decode(errors="replace") for record in answer]


def get_txt_lookup() -> TxtLookup:
    """FastAPI dependency; tests override it."""
    return txt_records
