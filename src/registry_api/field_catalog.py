"""Almena's field catalogue: what a field is, the schema its answer meets and
how it is served.

The fields themselves are data: the trust anchor's (`registry_api.trust_anchor`)
are Almena's catalogue, everyone's; each tenant adds its own for its forms
(`custom_fields`). Each of the anchor's fields has a stable id named as the
standards name it (OIDC Standard Claims, the EU PID rule book and its SD-JWT VC
encoding, ISO/IEC 18013-5 and 23220 for documents, schema.org for the rest),
the JSON Schema 2020-12 its answer must meet, and, when its answer is a code,
the value domain it comes from (ISO 3166-1, ISO 639, ISO 5218, ISCED 2011, IANA
media types…). Labels come in every language the portal speaks, kept apart
from the meaning as OCA overlays are.

Groups gather fields that only make sense together (an address, an identity
document); their parts are not offered alone. The ids are also the claim
names an issuer uses in the credential it grants.

The catalogue is versioned: a published version never changes meaning; new
fields may be added to it, and anything else is a new version.

Published on the identity domain (``{did_url}/schemas/fields/v1``, each field's
schema at ``…/v1/{id}.json``) and in the API (``/api/v1/catalog/fields``).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from registry_api.config import get_settings

VERSION = "v1"
LANGUAGES = ("en", "es")

Labels = dict[str, str]
FieldType = Literal["text", "email", "phone", "date", "code", "codes", "file", "group"]
# How a form may make a field stricter, by type; nothing may make it looser.
NARROWING: dict[str, set[str]] = {
    "text": {"max_length"},
    "date": {"min_date", "max_date"},
    "code": {"values"},
    "codes": {"values"},
    "file": {"values"},
}

E164 = r"^\+[1-9][0-9]{6,14}$"


@dataclass(frozen=True)
class Code:
    value: str | int
    labels: Labels
    # File formats: the media type a file in this format comes as.
    media_type: str | None = None


@dataclass(frozen=True)
class Domain:
    id: str
    labels: Labels
    source: str
    codes: tuple[Code, ...]


@dataclass(frozen=True)
class Part:
    key: str
    field: "Field"
    required: bool = True


@dataclass(frozen=True)
class Field:
    id: str
    type: FieldType
    labels: Labels
    source: str
    category: str = ""
    max_length: int | None = None
    pattern: str | None = None
    # `code`, `codes` and `file`: where the values come from.
    domain: str | None = None
    parts: tuple[Part, ...] = ()
    # May be asked for more than once in a form, each time under a name of
    # its own (`as`): files, so far.
    repeatable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # A coded field may carry its values itself (`options`), not a domain's.
    codes: tuple[Code, ...] = ()
    # The anchor's fields publish their schema; a tenant's own do not.
    published: bool = True
    # The codes of its `domain`, resolved as the catalogue is loaded.
    domain_codes: tuple[Code, ...] = ()


def code_of(data: dict[str, Any]) -> Code:
    return Code(data["value"], data["labels"], data.get("media_type"))


def field_from(
    ref: str,
    type: str,
    labels: Labels,
    definition: dict[str, Any],
    domains: dict[str, Domain],
    source: str = "",
    category: str = "",
    published: bool = True,
) -> Field:
    """A field as kept (its `definition`), its domain's codes resolved. A
    tenant's file field names its `formats`, which are file formats'."""
    domain = definition.get("domain")
    values = definition.get("values")
    if "formats" in definition:
        domain, values = "file_format", definition["formats"]
    found = domains.get(domain) if domain else None
    parts = tuple(
        Part(
            part["key"],
            field_from(
                part["key"],
                part["field"]["type"],
                part["field"]["labels"],
                part["field"].get("definition", {}),
                domains,
                part["field"].get("source", ""),
            ),
            part.get("required", True),
        )
        for part in definition.get("parts", [])
    )
    return Field(
        ref,
        cast(FieldType, type),
        labels,
        source,
        category,
        max_length=definition.get("max_length"),
        pattern=definition.get("pattern"),
        domain=domain,
        parts=parts,
        repeatable=bool(definition.get("repeatable")),
        extra={"values": tuple(values)} if values is not None else {},
        codes=tuple(code_of(option) for option in definition.get("options", [])),
        published=published,
        domain_codes=found.codes if found else (),
    )


def codes_of(item: Field) -> tuple[Code, ...]:
    """The codes a coded or file field draws on: its own, or its domain's."""
    return item.codes or item.domain_codes


def domain_values(item: Field) -> list[str | int]:
    """The values a coded or file field takes, before any form narrows them."""
    if item.codes:
        return [code.value for code in item.codes]
    allowed = item.extra.get("values")
    return [code.value for code in item.domain_codes if allowed is None or code.value in allowed]


def media_types(item: Field, values: list[str | int]) -> list[str]:
    """The media types the file formats `values` of a file field come as."""
    by_value = {code.value: code.media_type for code in item.domain_codes}
    return [str(by_value[value]) for value in values]


# JSON Schema -----------------------------------------------------------------


def schema_url(field_id: str) -> str:
    return f"{get_settings().did_url}/schemas/fields/{VERSION}/{field_id}.json"


def value_schema(item: Field, values: list[str | int] | None = None) -> dict[str, Any]:
    """The JSON Schema an answer to `item` meets; `values` narrows a coded one."""
    match item.type:
        case "text":
            schema: dict[str, Any] = {"type": "string", "minLength": 1}
            if item.max_length:
                schema["maxLength"] = item.max_length
            if item.pattern:
                schema["pattern"] = item.pattern
            return schema
        case "email":
            return {"type": "string", "format": "email", "maxLength": 254}
        case "phone":
            return {"type": "string", "pattern": item.pattern or E164}
        case "date":
            return {"type": "string", "format": "date"}
        case "code":
            return {"enum": values or domain_values(item)}
        case "codes":
            return {
                "type": "array",
                "items": {"enum": values or domain_values(item)},
                "minItems": 1,
                "uniqueItems": True,
            }
        case "file":
            # The file travels on its own; the answer names it and pins it by digest.
            return {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "maxLength": 255},
                    "media_type": {"enum": media_types(item, values or domain_values(item))},
                    "size": {"type": "integer", "minimum": 1},
                    "digest": {"type": "string", "pattern": "^sha256-[A-Za-z0-9+/]{43}=$"},
                },
                "required": ["media_type", "size", "digest"],
                "additionalProperties": False,
            }
        case "group":
            return {
                "type": "object",
                "properties": {part.key: value_schema(part.field) for part in item.parts},
                "required": [part.key for part in item.parts if part.required],
                "additionalProperties": False,
            }


def label_of_texts(texts: Labels, fallback: str) -> str:
    """The text in English, else in the first language it has."""
    return texts.get("en") or next(iter(texts.values()), fallback)


def label_of(item: Field) -> str:
    """Its label in English, else in the first language it has."""
    return label_of_texts(item.labels, item.id)


def field_schema(item: Field) -> dict[str, Any]:
    """The field's published JSON Schema document."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(item.id),
        "title": label_of(item),
        "$comment": f"Almena field catalogue {VERSION}; source: {item.source}",
        **value_schema(item),
    }


# The catalogue, as served ----------------------------------------------------


def field_out(item: Field) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": item.id,
        "type": item.type,
        "labels": item.labels,
        "source": item.source,
    }
    if item.category:
        out["category"] = item.category
        if item.published:
            out["schema"] = schema_url(item.id)
        out["narrowing"] = sorted(NARROWING.get(item.type, set()))
        out["repeatable"] = item.repeatable
    if item.domain:
        out["domain"] = item.domain
        if "values" in item.extra:
            out["values"] = list(item.extra["values"])
    if item.codes:
        out["codes"] = [_code_out(code) for code in item.codes]
    if item.max_length:
        out["max_length"] = item.max_length
    if item.pattern and not item.published:
        out["pattern"] = item.pattern
    if item.parts:
        out["parts"] = [
            {"key": part.key, "required": part.required, "field": field_out(part.field)}
            for part in item.parts
        ]
    return out


def _code_out(code: Code) -> dict[str, Any]:
    out: dict[str, Any] = {"value": code.value, "labels": code.labels}
    if code.media_type:
        out["media_type"] = code.media_type
    return out


def catalogue_out(
    fields: list[Field], domains: dict[str, Domain], categories: dict[str, Labels]
) -> dict[str, Any]:
    """The catalogue as served: its fields in order, their categories and the
    value domains they draw on."""
    return {
        "version": VERSION,
        "languages": list(LANGUAGES),
        "categories": [{"id": key, "labels": labels} for key, labels in categories.items()],
        "fields": [field_out(item) for item in fields],
        "domains": {
            domain.id: {
                "labels": domain.labels,
                "source": domain.source,
                "codes": [_code_out(code) for code in domain.codes],
            }
            for domain in domains.values()
        },
    }
