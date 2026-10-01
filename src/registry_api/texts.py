"""Texts written in several languages: what a tenant writes for people to read
in its forms — a form's name and description, a field's help, why a credential
is asked for — and its own fields' labels.

Each is an object by language, `{"en": …, "es": …}`, as the catalogue's labels
are, in the portal's languages (`field_catalog.LANGUAGES`); none is required to
be in all of them. Whoever reads it gets their language, else English, else
whichever there is (`text_of`).
"""

from typing import Any

from registry_api.field_catalog import LANGUAGES

Texts = dict[str, str]


def clean(value: Any, max_length: int) -> Texts:
    """The texts trimmed, the empty ones dropped; refused when not an object
    by the portal's languages or when one is longer than `max_length`."""
    if not isinstance(value, dict):
        raise ValueError("expected an object by language")
    if set(value) - set(LANGUAGES):
        raise ValueError(f"languages are {', '.join(LANGUAGES)}")
    kept = {lang: str(text).strip() for lang, text in value.items() if str(text or "").strip()}
    if any(len(text) > max_length for text in kept.values()):
        raise ValueError(f"at most {max_length} characters")
    return kept


def text_of(texts: Texts | None, lang: str = "en") -> str:
    """The text in `lang`, else English, else whichever there is."""
    if not texts:
        return ""
    return texts.get(lang) or texts.get("en") or next(iter(texts.values()), "")
