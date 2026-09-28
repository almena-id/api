"""What a certification request is checked against: its domain and its logo."""

import re
import secrets
from collections.abc import Awaitable, Callable

import dns.asyncresolver
import dns.exception
import dns.resolver

# Where the proof is published, and how it starts.
DNS_LABEL = "_almena"
DNS_PREFIX = "almena-verify="
LOGO_MAX_BYTES = 256 * 1024

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class CertificationError(Exception):
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
        raise CertificationError("domain_invalid") from None
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise CertificationError("domain_invalid")
    if labels[-1].isdigit():
        raise CertificationError("domain_invalid")
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
        raise CertificationError("dns_unavailable") from None
    return [b"".join(record.strings).decode(errors="replace") for record in answer]


def get_txt_lookup() -> TxtLookup:
    """FastAPI dependency; tests override it."""
    return txt_records


def logo_type(data: bytes) -> str:
    """The logo's media type, read from its bytes rather than from what it claims."""
    if len(data) > LOGO_MAX_BYTES:
        raise CertificationError("logo_too_large")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise CertificationError("logo_invalid")
