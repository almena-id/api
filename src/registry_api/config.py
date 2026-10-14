"""Settings, read from ``REGISTRY_*`` environment variables (and ``.env``)."""

from functools import lru_cache
from ipaddress import ip_address
from typing import Literal

from pydantic import Field, IPvAnyAddress, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
