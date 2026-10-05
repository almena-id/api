from email.message import EmailMessage

import pytest

from registry_api.config import get_settings
from registry_api.mail import MARK_CID, Mailer


class _Captured(Mailer):
    """Builds the message as for SMTP and keeps it."""

    def __init__(self) -> None:
        super().__init__(get_settings())
        self.messages: list[EmailMessage] = []

    def _send(self, message: EmailMessage) -> None:
        self.messages.append(message)


async def test_the_code_goes_as_text_and_html_with_the_mark() -> None:
    mailer = _Captured()
    await mailer.send_code("ada@example.org", "123456", "en")
    message = mailer.messages[-1]
    assert message.get_content_type() == "multipart/alternative"
    text = message.get_body(("plain",))
    html = message.get_body(("html",))
    assert text is not None and html is not None
    assert "123456" in text.get_content()
    assert "123456" in html.get_content()
    assert f"cid:{MARK_CID}" in html.get_content()
    related = message.get_body(("related",))
    assert related is not None
    images = [p for p in related.iter_parts() if p.get_content_type() == "image/png"]
    assert len(images) == 1 and images[0]["Content-ID"] == f"<{MARK_CID}>"


@pytest.mark.parametrize("locale", ["en", "es"])
async def test_the_html_is_ready_for_dark_mode(locale: str) -> None:
    mailer = _Captured()
    await mailer.send_code("ada@example.org", "123456", locale)  # type: ignore[arg-type]
    html = mailer.messages[-1].get_body(("html",))
    assert html is not None
    content = html.get_content()
    assert f'lang="{locale}"' in content
    assert 'name="color-scheme" content="light dark"' in content
    assert "prefers-color-scheme:dark" in content


async def test_an_invitation_escapes_what_people_named() -> None:
    mailer = _Captured()
    await mailer.send_invitation(
        "bob@example.org", tenant="<b>Club</b>", inviter="Ada & co", role="admin", locale="en"
    )
    html = mailer.messages[-1].get_body(("html",))
    assert html is not None
    content = html.get_content()
    assert "<b>Club</b>" not in content
    assert "&lt;b&gt;Club&lt;/b&gt;" in content
    assert "Ada &amp; co" in content
    assert f'href="{get_settings().portal_url.rstrip("/")}/login"' in content
