"""Settings, read from ``REGISTRY_*`` environment variables (and ``.env``)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
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
    # Proxies whose X-Forwarded-* headers are trusted (comma-separated IPs or "*").
    forwarded_allow_ips: str = "127.0.0.1"

    # Public origin of this API, advertised as the server in the OpenAPI document.
    public_url: str = "https://api.almena.network"
    # Origins allowed by CORS: the registry portal.
    cors_origins: list[str] = Field(default_factory=lambda: ["https://registry.almena.network"])

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "registry"
    db_user: str = "registry"
    db_password: SecretStr = SecretStr("registry")
    db_pool_size: int = 5
    db_echo: bool = False

    # How long a portal sign-in lasts before a new code is needed.
    session_ttl_hours: int = Field(default=168, gt=0)
    # Emailed sign-in codes: lifetime and wrong guesses allowed per code.
    login_code_ttl_minutes: int = Field(default=10, gt=0)
    login_code_max_attempts: int = Field(default=5, gt=0)

    # Outgoing mail. Development points at Mailpit (localhost:1025, no auth).
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = False
    mail_from: str = "Almena Registry <no-reply@almena.network>"

    # Public origin of the portal: providers send the browser back to
    # `{portal_url}/auth/{provider}/callback`, which is what each one registers.
    portal_url: str = "https://registry.almena.network"
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
