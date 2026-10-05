"""Proving a domain with DNS: a TXT record the owner publishes.

A tenant links a domain (`api/routes/domains.py`) and gets the record that
proves it: ``_almena.{domain}`` saying ``almena-verify={token}``. The token is
the same every time the tenant links that domain, so a record published once
keeps proving it after the domain is removed and linked again. The record
is looked up at public resolvers (``REGISTRY_DNS_RESOLVERS``), each asked on
its own, so that one still caching its absence does not hide it.
"""

import asyncio
import base64
import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable

import dns.asyncresolver
import dns.exception
import dns.resolver

from registry_api.config import get_settings

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


def new_token(tenant_id: uuid.UUID, domain: str) -> str:
    """The tenant's token for `domain`: always the same for that pair, another
    for any other tenant. It need not be secret: only whoever runs the domain's
    DNS can publish it."""
    digest = hashlib.sha256(f"almena-verify:{tenant_id}:{domain}".encode()).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode()


def dns_record(domain: str, token: str) -> tuple[str, str]:
    """The TXT record that proves control of `domain`: its name and its value."""
    return f"{DNS_LABEL}.{domain}", f"{DNS_PREFIX}{token}"


TxtLookup = Callable[[str], Awaitable[list[str]]]


async def _txt_at(nameserver: str, name: str) -> list[str] | None:
    """The TXT records at `name` as one resolver sees them; None when it times out."""
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [nameserver]
    try:
        answer = await resolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.Timeout:
        return None
    return [b"".join(record.strings).decode(errors="replace") for record in answer]


async def txt_records(name: str) -> list[str]:
    """The TXT records at `name`, as every public resolver sees them together;
    none when it does not exist. Unavailable only when none answers."""
    nameservers = [str(address) for address in get_settings().dns_resolvers]
    answers = await asyncio.gather(*(_txt_at(server, name) for server in nameservers))
    if all(answer is None for answer in answers):
        raise DomainError("dns_unavailable")
    return list(dict.fromkeys(record for answer in answers if answer for record in answer))


def get_txt_lookup() -> TxtLookup:
    """FastAPI dependency; tests override it."""
    return txt_records
