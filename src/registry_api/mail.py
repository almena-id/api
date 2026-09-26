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

    async def send_code(self, to: str, code: str, locale: Locale) -> None:
        subject, body = _CODE_MAIL[locale]
        minutes = self.settings.login_code_ttl_minutes
        await self.send(to, subject.format(code=code), body.format(code=code, minutes=minutes))


def get_mailer() -> Mailer:
    """FastAPI dependency; tests override it to catch what would be sent."""
    return Mailer(get_settings())
