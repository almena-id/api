"""Sign-in codes and session tokens."""

import hashlib
import secrets


def new_code() -> str:
    """A six-digit sign-in code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def new_token() -> str:
    """An opaque bearer token: 256 random bits."""
    return secrets.token_urlsafe(32)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()
