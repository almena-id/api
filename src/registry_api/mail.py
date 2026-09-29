"""Outgoing email over SMTP (Mailpit in development)."""

import smtplib
from email.message import EmailMessage
from typing import Literal

from starlette.concurrency import run_in_threadpool

from registry_api.config import Settings, get_settings

Locale = Literal["en", "es"]

_CODE_MAIL: dict[Locale, tuple[str, str]] = {
    "en": (
        "Your Almena Registry code: {code}",
        "Your sign-in code for Almena Registry is:\n\n    {code}\n\n"
        "It expires in {minutes} minutes. If you did not ask for it, ignore this email.\n",
    ),
    "es": (
        "Tu código de Almena Registry: {code}",
        "Tu código para entrar en Almena Registry es:\n\n    {code}\n\n"
        "Caduca en {minutes} minutos. Si no lo has pedido, ignora este correo.\n",
    ),
}


_INVITATION_MAIL: dict[Locale, tuple[str, str]] = {
    "en": (
        "You have been invited to {tenant} on Almena Registry",
        "{inviter} has invited you to {tenant} on Almena Registry, as {role}.\n\n"
        "Sign in with this email address to join:\n\n    {url}\n",
    ),
    "es": (
        "Te han invitado a {tenant} en Almena Registry",
        "{inviter} te ha invitado a {tenant} en Almena Registry, como {role}.\n\n"
        "Inicia sesión con esta dirección de correo para unirte:\n\n    {url}\n",
    ),
}

_ROLE_NAMES: dict[Locale, dict[str, str]] = {
    "en": {"admin": "admin", "member": "member"},
    "es": {"admin": "administrador", "member": "miembro"},
}

_UNNAMED_TENANT: dict[Locale, str] = {"en": "a tenant", "es": "un tenant"}

# An inviter whose account has neither alias nor email.
_SOMEONE: dict[Locale, str] = {"en": "Someone", "es": "Alguien"}


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _send(self, message: EmailMessage) -> None:
        s = self.settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.mail_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        await run_in_threadpool(self._send, message)

    async def send_invitation(
        self,
        to: str,
        *,
        tenant: str | None,
        inviter: str | None,
        role: str,
        locale: Locale,
    ) -> None:
        subject, body = _INVITATION_MAIL[locale]
        values = {
            "tenant": tenant or _UNNAMED_TENANT[locale],
            "inviter": inviter or _SOMEONE[locale],
            "role": _ROLE_NAMES[locale].get(role, role),
            "url": f"{self.settings.portal_url.rstrip('/')}/login",
        }
        await self.send(to, subject.format(**values), body.format(**values))

    async def send_code(self, to: str, code: str, locale: Locale) -> None:
        subject, body = _CODE_MAIL[locale]
        minutes = self.settings.login_code_ttl_minutes
        await self.send(to, subject.format(code=code), body.format(code=code, minutes=minutes))


def get_mailer() -> Mailer:
    """FastAPI dependency; tests override it to catch what would be sent."""
    return Mailer(get_settings())
