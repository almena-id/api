"""Settings, read from ``REGISTRY_*`` environment variables (and ``.env``)."""

import re
from functools import lru_cache
from ipaddress import ip_address
from typing import Annotated, Literal

from pydantic import Field, IPvAnyAddress, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# An Apple app: its team (ten characters) and bundle identifier.
_APPLE_APP_ID = r"[A-Z0-9]{10}\.[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*"
# An Android application id: dot-separated Java identifiers, two at least.
_ANDROID_PACKAGE = r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$"
# A certificate's SHA-256 fingerprint as keytool and Play Console print it.
_FINGERPRINT = r"[0-9A-F]{2}(:[0-9A-F]{2}){31}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REGISTRY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # One JSON object per line (containers) or text for a terminal.
    log_format: Literal["json", "text"] = "json"
    # Proxies whose X-Forwarded-* headers are trusted (comma-separated IPs or "*").
    forwarded_allow_ips: str = "127.0.0.1"

    # Public origin of this API, advertised as the server in the OpenAPI document.
    public_url: str = "https://api.almena.id"
    # Origin of the identity domain: identities' did:web DIDs are made from it
    # and resolve there, and the wallet's channel lives there (wallet.channel_url);
    # it proxies /ids/, /.well-known/, /schemas/ and those paths to this API.
    did_url: str = "https://almena.id"
    # Origins allowed by CORS: the registry portal.
    cors_origins: list[str] = Field(default_factory=lambda: ["https://registry.almena.id"])
    # Directory holding the origin's `did-configuration.json`, served under
    # /.well-known/; empty serves none. (Its `did.json` is the root tenant's.)
    well_known_dir: str = ""
    # Public resolvers asked for a domain's TXT proof (registry_api.dns_proof),
    # each on its own, instead of the system's: a local resolver can cache the
    # record's absence from a check made before it was published.
    dns_resolvers: list[IPvAnyAddress] = Field(
        default_factory=lambda: [ip_address("1.1.1.1"), ip_address("8.8.8.8")],
        min_length=1,
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "registry"
    db_user: str = "registry"
    db_password: SecretStr = SecretStr("registry")
    db_pool_size: int = 5
    db_echo: bool = False

    # A portal sign-in ends this long after it started, used or not…
    session_ttl_hours: int = Field(default=12, gt=0)
    # …or earlier, once no request has used it for this long.
    session_idle_minutes: int = Field(default=30, gt=0)
    # Emailed sign-in codes: lifetime and wrong guesses allowed per code.
    login_code_ttl_minutes: int = Field(default=10, gt=0)
    login_code_max_attempts: int = Field(default=5, gt=0)
    # Per address (registry_api.throttle): this long at least between two codes,
    # and so many an hour at most; 0 lifts either.
    login_code_resend_seconds: int = Field(default=60, ge=0)
    login_codes_per_hour: int = Field(default=5, ge=0)
    # Wrong codes per address, whichever codes they were for: so many lock it
    # for `login_lock_minutes`, and a day takes no more than `login_failures_per_day`.
    login_lock_failures: int = Field(default=5, ge=0)
    login_lock_minutes: int = Field(default=15, gt=0)
    login_failures_per_day: int = Field(default=20, ge=0)
    # Per network address, an hour: codes asked for, wrong codes, applications
    # started, wallet sign-ins asked for; 0 lifts the limit.
    ip_login_codes_per_hour: int = Field(default=20, ge=0)
    ip_login_failures_per_hour: int = Field(default=30, ge=0)
    ip_applications_per_hour: int = Field(default=30, ge=0)
    ip_wallet_requests_per_hour: int = Field(default=60, ge=0)
    # Shared with the portals (registry, catalog), which call this API from their
    # servers: with it they name their visitor's address, so the limits above
    # count visitors rather than the portal. Empty: no caller may name one.
    portal_secret: SecretStr = SecretStr("")

    # Days whoever holds a sent application's secret can still open it, after
    # its latest step (sent, decided, issued, received): to read the issuer's
    # decision, made by hand, and take the credential.
    application_holder_days: int = Field(default=90, gt=0)

    # Every so many minutes, what has run out is deleted (registry_api.housekeeping); 0 never.
    housekeeping_minutes: int = Field(default=15, ge=0)

    # Outgoing mail. Development points at Mailpit (localhost:1025, no auth).
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = False
    mail_from: str = "Almena Registry <no-reply@almena.id>"

    # The broker issuers' and verifiers' back offices read their queues from
    # (registry_api.broker). The API manages it through the management HTTP
    # API, as the platform's own account, in one virtual host; tenants connect
    # over AMQP at `rabbitmq_amqp_url`, which is what the portal shows them.
    rabbitmq_api_url: str = "http://localhost:15672"
    rabbitmq_admin: str = "almena"
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_vhost: str = "almena"
    rabbitmq_amqp_url: str = "amqps://broker.almena.id:5671"

    # Public origin of the catalog, the citizens' portal where holders apply
    # for credentials: the wallet answers it (`client_id`) when applying.
    catalog_url: str = "https://catalog.almena.id"

    # Public origin of the portal: providers send the browser back to
    # `{portal_url}/auth/{provider}/callback`, which is what each one registers.
    portal_url: str = "https://registry.almena.id"
    # Social sign-in. A provider without its client id (and secret or key) is off.
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    microsoft_client_id: str = ""
    microsoft_client_secret: SecretStr = SecretStr("")
    # `common` takes work, school and personal accounts; a tenant id narrows it.
    microsoft_tenant: str = "common"
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    # Apple: the Services ID, the team, and the Sign in with Apple key (.p8, PEM).
    apple_client_id: str = ""
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: SecretStr = SecretStr("")

    # The wallet's links on the identity domain (`{did_url}/w/…`, universal
    # links and verified App Links): the apps that may open them. Apple's
    # `{team}.{bundle id}`, served in /.well-known/apple-app-site-association;
    # Android's package and the SHA-256 fingerprints of the certificates its
    # builds are signed with (Play App Signing's key, and the upload or debug
    # key to try a build of one's own), in /.well-known/assetlinks.json. Both
    # comma-separated; no fingerprint serves an empty list, so Android never
    # verifies the links and leaves them to the browser.
    apple_app_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["XDUFSW425R.id.almena.wallet"]
    )
    android_package: str = Field(default="id.almena.wallet", pattern=_ANDROID_PACKAGE)
    android_app_fingerprints: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Where someone without the wallet learns how to get it: the page at
    # `{did_url}/w/…` links it.
    wallet_url: str = "https://docs.almena.id/use/wallet/"

    @field_validator("apple_app_ids", "android_app_fingerprints", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("apple_app_ids")
    @classmethod
    def _apple_app_ids(cls, value: list[str]) -> list[str]:
        for app_id in value:
            if not re.fullmatch(_APPLE_APP_ID, app_id):
                raise ValueError(f"not a {{team id}}.{{bundle id}}: {app_id!r}")
        return value

    @field_validator("android_app_fingerprints")
    @classmethod
    def _android_app_fingerprints(cls, value: list[str]) -> list[str]:
        for fingerprint in value:
            if not re.fullmatch(_FINGERPRINT, fingerprint):
                raise ValueError(
                    f"not a SHA-256 fingerprint (AA:BB:…, 32 uppercase pairs): {fingerprint!r}"
                )
        return value

    @model_validator(mode="after")
    def _safe_in_production(self) -> "Settings":
        """In production, nothing left as the development defaults that would
        open it: the database's password must be set."""
        if self.environment == "production" and self.db_password.get_secret_value() in (
            "",
            "registry",
        ):
            raise ValueError("REGISTRY_DB_PASSWORD must be set in production")
        return self

    @property
    def database_url(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.db_user,
                password=self.db_password.get_secret_value(),
                host=self.db_host,
                port=self.db_port,
                path=self.db_name,
            )
        )

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
