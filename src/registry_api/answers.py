"""Checking what a holder types into a form, field by field.

A form's fields are the catalogue's (Almena's, or the tenant's own); each
answer must meet its field's format — the type, its bounds, its value domain —
and what the form narrows. Fields a verified credential filled are not typed:
their values come from the credential. Files are uploaded on their own and
answered by what describes them (name, media type, size, digest).

The checks are the ones the form's JSON Schema states, made here so each
problem names its field: `required`, `format` (not what the type says), `range`
(outside its bounds or dates), `value` (not one of its values), `file` (not
uploaded). A group's parts are reported as `{field}.{part}`.
"""

import re
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import field_catalog as catalog
from registry_api.api.routes.custom_fields import PREFIX, custom_catalog
from registry_api.models import Form

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def key_of(stored: dict[str, Any]) -> str:
    """A form field's key in the answers."""
    return str(stored.get("as") or str(stored["ref"]).removeprefix(PREFIX))


async def fields_of(
    db: AsyncSession, form: Form
) -> list[tuple[str, dict[str, Any], catalog.Field]]:
    """The form's fields in order: their key, as the form keeps them, and the
    catalogue field each is."""
    custom = await custom_catalog(db, form.tenant_id)
    found = []
    for stored in form.fields:
        item = catalog.BY_ID.get(stored["ref"]) or custom.get(stored["ref"])
        if item is not None:
            found.append((key_of(stored), stored, item))
    return found


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def value_of(
    item: catalog.Field, narrow: dict[str, Any], value: Any, files: dict[str, Any], key: str
) -> tuple[Any, str | None]:
    """The answer as kept, or what is wrong with it."""
    match item.type:
        case "text":
            if not isinstance(value, str):
                return None, "format"
            text = value.strip()
            limit = narrow.get("max_length") or item.max_length
            if limit and len(text) > limit:
                return None, "range"
            if item.pattern and not re.fullmatch(item.pattern, text):
                return None, "format"
            return text, None
        case "email":
            text = str(value).strip()
            return (text, None) if EMAIL.match(text) and len(text) <= 254 else (None, "format")
        case "phone":
            text = re.sub(r"[\s\-().]", "", str(value))
            return (text, None) if re.fullmatch(catalog.E164, text) else (None, "format")
        case "date":
            try:
                day = date.fromisoformat(str(value))
            except ValueError:
                return None, "format"
            low, high = narrow.get("min_date"), narrow.get("max_date")
            if (low and day < date.fromisoformat(low)) or (high and day > date.fromisoformat(high)):
                return None, "range"
            return day.isoformat(), None
        case "code" | "codes":
            allowed = narrow.get("values") or catalog.domain_values(item)
            by_text = {str(code): code for code in allowed}
            chosen = value if item.type == "codes" else [value]
            if not isinstance(chosen, list) or not chosen:
                return None, "value"
            picked = [by_text.get(str(one)) for one in chosen]
            if any(one is None for one in picked) or len(set(map(str, chosen))) != len(chosen):
                return None, "value"
            return (picked if item.type == "codes" else picked[0]), None
        case "file":
            meta = files.get(key)
            return (meta, None) if meta else (None, "file")
        case "group":
            if not isinstance(value, dict):
                return None, "format"
            kept: dict[str, Any] = {}
            for part in item.parts:
                given = value.get(part.key)
                if _empty(given):
                    if part.required:
                        return None, f"{part.key}:required"
                    continue
                answer, problem = value_of(part.field, {}, given, files, key)
                if problem:
                    return None, f"{part.key}:{problem}"
                kept[part.key] = answer
            return kept, None
    return None, "format"


def check(
    fields: list[tuple[str, dict[str, Any], catalog.Field]],
    answers: dict[str, Any],
    files: dict[str, Any],
    filled: set[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """The answers as kept and, by field key, what is wrong."""
    kept: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, stored, item in fields:
        if key in filled:
            continue
        given = files.get(key) if item.type == "file" else answers.get(key)
        if _empty(given):
            if stored.get("required", True):
                errors[key] = "required"
            continue
        value, problem = value_of(item, stored.get("narrow", {}), given, files, key)
        if problem:
            part, _, code = problem.rpartition(":")
            errors[f"{key}.{part}" if part else key] = code
        else:
            kept[key] = value
    return kept, errors
