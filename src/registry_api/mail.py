"""Outgoing email over SMTP (Mailpit in development).

Each email goes as plain text with an HTML alternative. The HTML is light by
default and turns dark where the client says the reader prefers it
(`prefers-color-scheme`, Outlook.com's `[data-ogsc]`); its colours also stand
a client's own inversion (Gmail's). The registry's green mark travels inline
(`cid:`), since mail clients do not draw SVG.
"""

import smtplib
from email.message import EmailMessage
from html import escape
from pathlib import Path
from string import Template
from typing import Literal

from starlette.concurrency import run_in_threadpool

from registry_api.config import Settings, get_settings

Locale = Literal["en", "es"]

MARK_CID = "mark@almena.id"

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

# The HTML version: heading, lead, the note under the code.
_CODE_HTML: dict[Locale, tuple[str, str, str]] = {
    "en": (
        "Your sign-in code",
        "Use this code to sign in to Almena Registry.",
        "It expires in {minutes} minutes. If you did not ask for it, you can safely "
        "ignore this email.",
    ),
    "es": (
        "Tu código de acceso",
        "Usa este código para entrar en Almena Registry.",
        "Caduca en {minutes} minutos. Si no lo has pedido, puedes ignorar este correo.",
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

# The HTML version: heading, lead (with {inviter}, {tenant}, {role}), the hint
# above the button, the button, the note with the link spelled out.
_INVITATION_HTML: dict[Locale, tuple[str, str, str, str, str]] = {
    "en": (
        "You're invited to {tenant}",
        "{inviter} has invited you to join {tenant} on Almena Registry as {role}.",
        "Sign in with this email address to accept.",
        "Join {tenant}",
        "If the button does not work, open this link:",
    ),
    "es": (
        "Te han invitado a {tenant}",
        "{inviter} te ha invitado a unirte a {tenant} en Almena Registry como {role}.",
        "Inicia sesión con esta dirección de correo para aceptar.",
        "Unirme a {tenant}",
        "Si el botón no funciona, abre este enlace:",
    ),
}

# The footer: why this email came, and who sends it.
_FOOTER: dict[Locale, str] = {
    "en": "You are receiving this email because this address was used on Almena Registry.",
    "es": "Recibes este correo porque esta dirección se ha usado en Almena Registry.",
}

_ROLE_NAMES: dict[Locale, dict[str, str]] = {
    "en": {"admin": "admin", "member": "member"},
    "es": {"admin": "administrador", "member": "miembro"},
}

_UNNAMED_TENANT: dict[Locale, str] = {"en": "a tenant", "es": "un tenant"}

# An inviter whose account has neither alias nor email.
_SOMEONE: dict[Locale, str] = {"en": "Someone", "es": "Alguien"}

# Typefaces as the portals', with system fallbacks: few mail clients load fonts.
_FONT_BRAND = "'Chakra Petch',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
_FONT_SANS = "Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_FONT_MONO = "'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# The frame every email shares (the mark and wordmark, a card, the footer)
# lives in `assets/`: mail.html, a `string.Template`, and mail.css, light by
# default with the dark overrides.
_ASSETS = Path(__file__).parent / "assets"
_TEMPLATE = Template((_ASSETS / "mail.html").read_text(encoding="utf-8"))
_CSS = Template((_ASSETS / "mail.css").read_text(encoding="utf-8"))


def _layout(locale: Locale, title: str, preheader: str, content: str, fonts: str) -> str:
    return _TEMPLATE.substitute(
        lang=locale,
        title=title,
        css=_CSS.substitute(fonts=fonts),
        preheader=preheader,
        mark=MARK_CID,
        font_brand=_FONT_BRAND,
        font_sans=_FONT_SANS,
        content=content,
        footer=_FOOTER[locale],
    )


def _heading(text: str) -> str:
    return (
        f'<h1 class="text" style="margin:0 0 12px;font-family:{_FONT_BRAND};font-size:24px;'
        f'line-height:30px;font-weight:600;letter-spacing:-0.4px;color:#16181b;">{text}</h1>'
    )


def _paragraph(text: str, *, muted: bool = False, size: int = 15, bottom: int = 0) -> str:
    cls, color = ("muted", "#6b7075") if muted else ("text", "#2c3034")
    return (
        f'<p class="{cls}" style="margin:0 0 {bottom}px;font-size:{size}px;'
        f'line-height:{round(size * 1.6)}px;color:{color};">{text}</p>'
    )


def code_html(code: str, minutes: int, locale: Locale, fonts: str) -> str:
    heading, lead, note = _CODE_HTML[locale]
    content = (
        _heading(heading)
        + _paragraph(lead, bottom=24)
        + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td class="code-box" align="center" style="padding:22px 12px;background:#ecf7f0;'
        'border:1px solid #c6e8d3;border-radius:12px;">'
        f'<span class="code" style="font-family:{_FONT_MONO};font-size:36px;line-height:40px;'
        f'font-weight:700;letter-spacing:12px;color:#18804a;">{escape(code)}</span>'
        "</td></tr></table>"
        '<div style="height:24px;line-height:24px;font-size:0;">&nbsp;</div>'
        + _paragraph(note.format(minutes=minutes), muted=True, size=13)
    )
    return _layout(locale, escape(heading), escape(lead), content, fonts)


def invitation_html(
    *, tenant: str, inviter: str, role: str, url: str, locale: Locale, fonts: str
) -> str:
    heading, lead, hint, button, fallback = _INVITATION_HTML[locale]
    strong = '<strong class="text" style="color:#16181b;">{}</strong>'
    values = {
        "tenant": strong.format(escape(tenant)),
        "inviter": strong.format(escape(inviter)),
        "role": strong.format(escape(role)),
    }
    link = escape(url, quote=True)
    content = (
        _heading(heading.format(tenant=escape(tenant)))
        + _paragraph(lead.format(**values), bottom=8)
        + _paragraph(hint, muted=True, size=14, bottom=28)
        + '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td class="button" align="center" bgcolor="#1f9d55" style="border-radius:10px;'
        'background:#1f9d55;">'
        f'<a href="{link}" target="_blank" style="display:inline-block;padding:13px 26px;'
        f"font-family:{_FONT_SANS};font-size:15px;line-height:20px;font-weight:600;"
        f'color:#ffffff;text-decoration:none;border-radius:10px;">'
        f"{escape(button.format(tenant=tenant))}</a>"
        "</td></tr></table>"
        '<div class="rule" style="height:0;margin:32px 0 20px;border-top:1px solid #e3e7e5;">'
        "</div>"
        + _paragraph(fallback, muted=True, size=12, bottom=4)
        + f'<p style="margin:0;font-family:{_FONT_MONO};font-size:12px;line-height:18px;'
        f'word-break:break-all;"><a href="{link}" target="_blank" class="brand" '
        f'style="color:#18804a;text-decoration:none;">{link}</a></p>'
    )
    preheader = escape(lead.format(tenant=tenant, inviter=inviter, role=role))
    return _layout(locale, escape(heading.format(tenant=tenant)), preheader, content, fonts)


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def _fonts(self) -> str:
        """Where the HTML's typefaces are served from (the API's `/fonts/`)."""
        return f"{self.settings.public_url.rstrip('/')}/fonts"

    def _send(self, message: EmailMessage) -> None:
        s = self.settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send(self, to: str, subject: str, body: str, html: str | None = None) -> None:
        message = EmailMessage()
        message["From"] = self.settings.mail_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype="html")
            if f"cid:{MARK_CID}" in html:
                part = message.get_body(("html",))
                assert isinstance(part, EmailMessage)
                part.add_related(
                    (_ASSETS / "mail-mark.png").read_bytes(),
                    "image",
                    "png",
                    cid=f"<{MARK_CID}>",
                    filename="almena.png",
                )
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
        html = invitation_html(**values, locale=locale, fonts=self._fonts)
        await self.send(to, subject.format(**values), body.format(**values), html)

    async def send_code(self, to: str, code: str, locale: Locale) -> None:
        subject, body = _CODE_MAIL[locale]
        minutes = self.settings.login_code_ttl_minutes
        html = code_html(code, minutes, locale, self._fonts)
        await self.send(
            to, subject.format(code=code), body.format(code=code, minutes=minutes), html
        )


def get_mailer() -> Mailer:
    """FastAPI dependency; tests override it to catch what would be sent."""
    return Mailer(get_settings())
