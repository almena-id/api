"""Social sign-in providers: Google, Microsoft, Apple and GitHub.

Each one runs the authorization code flow with PKCE and a `state`; the OpenID
Connect ones (all but GitHub) also get a `nonce`. What a provider hands back is
reduced to an `Identity`: its stable id for the account, and an email with
whether the provider vouches for it. Only a vouched-for email may create an
account or join an existing one, or anyone could claim somebody else's address.

The ID tokens are read without checking their signature: they come straight
from the provider's token endpoint over TLS, which OpenID Connect Core §3.1.3.7
accepts in place of the signature. Issuer, audience, expiry and nonce are still
checked.
"""

import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import jwt

from registry_api.config import Settings

ProviderId = Literal["google", "microsoft", "apple", "github"]
PROVIDERS: tuple[ProviderId, ...] = ("google", "microsoft", "apple", "github")

# Microsoft's tenant for personal accounts (Outlook, Hotmail, Xbox…), whose
# addresses Microsoft has verified.
_MICROSOFT_CONSUMERS = "9188040d-6c67-4c5b-b112-36a304b66dad"


class OAuthError(Exception):
    """The provider refused, or answered something that cannot be trusted."""

    def __init__(self, code: str = "provider_error") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str | None
    email_verified: bool


def redirect_uri(settings: Settings, provider: ProviderId) -> str:
    return f"{settings.portal_url.rstrip('/')}/auth/{provider}/callback"


def is_enabled(settings: Settings, provider: ProviderId) -> bool:
    match provider:
        case "google":
            return bool(
                settings.google_client_id and settings.google_client_secret.get_secret_value()
            )
        case "microsoft":
            return bool(
                settings.microsoft_client_id and settings.microsoft_client_secret.get_secret_value()
            )
        case "github":
            return bool(
                settings.github_client_id and settings.github_client_secret.get_secret_value()
            )
        case "apple":
            return bool(
                settings.apple_client_id
                and settings.apple_team_id
                and settings.apple_key_id
                and settings.apple_private_key.get_secret_value()
            )


def _client_id(settings: Settings, provider: ProviderId) -> str:
    return {
        "google": settings.google_client_id,
        "microsoft": settings.microsoft_client_id,
        "github": settings.github_client_id,
        "apple": settings.apple_client_id,
    }[provider]


def _client_secret(settings: Settings, provider: ProviderId) -> str:
    match provider:
        case "google":
            return settings.google_client_secret.get_secret_value()
        case "microsoft":
            return settings.microsoft_client_secret.get_secret_value()
        case "github":
            return settings.github_client_secret.get_secret_value()
        case "apple":
            # Apple's client secret is a short-lived JWT signed with the team's key.
            now = int(time.time())
            return jwt.encode(
                {
                    "iss": settings.apple_team_id,
                    "iat": now,
                    "exp": now + 300,
                    "aud": "https://appleid.apple.com",
                    "sub": settings.apple_client_id,
                },
                # A PEM kept on one line in the environment writes its breaks as \\n.
                settings.apple_private_key.get_secret_value().replace("\\n", "\n"),
                algorithm="ES256",
                headers={"kid": settings.apple_key_id},
            )


def _microsoft_base(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0"


def authorization_url(
    settings: Settings, provider: ProviderId, *, state: str, challenge: str, nonce: str
) -> str:
    params = {
        "client_id": _client_id(settings, provider),
        "redirect_uri": redirect_uri(settings, provider),
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    match provider:
        case "google":
            base = "https://accounts.google.com/o/oauth2/v2/auth"
            params |= {"scope": "openid email", "nonce": nonce, "prompt": "select_account"}
        case "microsoft":
            base = f"{_microsoft_base(settings)}/authorize"
            params |= {"scope": "openid email profile", "nonce": nonce, "prompt": "select_account"}
        case "apple":
            base = "https://appleid.apple.com/auth/authorize"
            # Asking for the email makes Apple post the answer back (form_post).
            params |= {"scope": "email", "nonce": nonce, "response_mode": "form_post"}
        case "github":
            base = "https://github.com/login/oauth/authorize"
            params |= {"scope": "read:user user:email", "allow_signup": "true"}
    return f"{base}?{urlencode(params)}"


def _token_url(settings: Settings, provider: ProviderId) -> str:
    return {
        "google": "https://oauth2.googleapis.com/token",
        "microsoft": f"{_microsoft_base(settings)}/token",
        "apple": "https://appleid.apple.com/auth/token",
        "github": "https://github.com/login/oauth/access_token",
    }[provider]


def _claims(id_token: str, *, audience: str, nonce: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as error:
        raise OAuthError from error
    aud = claims.get("aud")
    if (audience not in aud) if isinstance(aud, list) else aud != audience:
        raise OAuthError
    if int(claims.get("exp", 0)) < time.time() or claims.get("nonce") != nonce:
        raise OAuthError
    if not claims.get("sub"):
        raise OAuthError
    return claims


def _truthy(value: object) -> bool:
    # Apple writes booleans in its ID tokens as strings.
    return value is True or value == "true"


async def exchange(
    settings: Settings,
    provider: ProviderId,
    http: httpx.AsyncClient,
    *,
    code: str,
    verifier: str,
    nonce: str,
) -> Identity:
    """Trade the code for tokens and read who signed in."""
    client_id = _client_id(settings, provider)
    try:
        response = await http.post(
            _token_url(settings, provider),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(settings, provider),
                "client_id": client_id,
                "client_secret": _client_secret(settings, provider),
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        tokens: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise OAuthError from error
    if response.status_code != 200 or "error" in tokens:
        raise OAuthError

    if provider == "github":
        return await _github_identity(http, str(tokens.get("access_token", "")))

    claims = _claims(str(tokens.get("id_token", "")), audience=client_id, nonce=nonce)
    email = claims.get("email")
    match provider:
        case "google":
            if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
                raise OAuthError
            verified = _truthy(claims.get("email_verified"))
        case "apple":
            if claims.get("iss") != "https://appleid.apple.com":
                raise OAuthError
            verified = _truthy(claims.get("email_verified"))
        case "microsoft":
            tid = str(claims.get("tid", ""))
            if claims.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0":
                raise OAuthError
            # A work or school account's email is whatever its admin typed, unless
            # the optional `xms_edov` claim says the domain is verified.
            verified = tid == _MICROSOFT_CONSUMERS or _truthy(claims.get("xms_edov"))
    return Identity(
        subject=str(claims["sub"]),
        email=str(email).lower() if email else None,
        email_verified=verified,
    )


async def _github_identity(http: httpx.AsyncClient, access_token: str) -> Identity:
    if not access_token:
        raise OAuthError
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        user = (await http.get("https://api.github.com/user", headers=headers)).json()
        emails = (await http.get("https://api.github.com/user/emails", headers=headers)).json()
    except (httpx.HTTPError, ValueError) as error:
        raise OAuthError from error
    if not isinstance(user, dict) or "id" not in user or not isinstance(emails, list):
        raise OAuthError
    primary = next(
        (e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")),
        None,
    )
    return Identity(
        subject=str(user["id"]),
        email=str(primary["email"]).lower() if primary else None,
        email_verified=primary is not None,
    )
