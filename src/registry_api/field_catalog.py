"""Almena's field catalogue: the only fields a form may ask for.

Tenants do not define fields; they pick them from here. Each field has a
stable id named as the standards name it (OIDC Standard Claims, the EU PID
rule book and its SD-JWT VC encoding, ISO/IEC 18013-5 and 23220 for documents,
schema.org for the rest), the JSON Schema 2020-12 its answer must meet, and,
when its answer is a code, the value domain it comes from (ISO 3166-1, ISO 639,
ISO 5218, ISCED 2011, IANA media types…). Labels come in every language the
portal speaks, kept apart from the meaning as OCA overlays are.

Groups gather fields that only make sense together (an address, an identity
document); their parts are not offered alone. The ids are also the claim
names an issuer uses in the credential it grants.

The catalogue is versioned: a published version never changes meaning; new
fields may be added to it, and anything else is a new version.

Published on the identity domain (``{did_url}/schemas/fields/v1``, each field's
schema at ``…/v1/{id}.json``) and in the API (``/api/v1/catalog/fields``).
"""

import gettext
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

import pycountry

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
    # A tenant's own coded field carries its values itself, not a domain's.
    codes: tuple[Code, ...] = ()
    # Almena's fields publish their schema; a tenant's own do not.
    published: bool = True


# Domains ---------------------------------------------------------------------


def _translation(domain: str, language: str) -> gettext.NullTranslations:
    if language == "en":
        return gettext.NullTranslations()
    return gettext.translation(domain, pycountry.LOCALES_DIR, languages=[language], fallback=True)


def _countries() -> tuple[Code, ...]:
    tr = {lang: _translation("iso3166-1", lang) for lang in LANGUAGES}
    codes = []
    for country in pycountry.countries:
        name = getattr(country, "common_name", None) or country.name
        labels = {}
        for lang in LANGUAGES:
            # The common name when translated, else the short official one.
            label = tr[lang].gettext(name)
            labels[lang] = (
                label if label != name or lang == "en" else tr[lang].gettext(country.name)
            )
        codes.append(Code(country.alpha_2, labels))
    return tuple(sorted(codes, key=lambda code: str(code.value)))


def _languages() -> tuple[Code, ...]:
    tr = {lang: _translation("iso639-3", lang) for lang in LANGUAGES}
    codes = [
        Code(language.alpha_2, {lang: tr[lang].gettext(language.name) for lang in LANGUAGES})
        for language in pycountry.languages
        if hasattr(language, "alpha_2")
    ]
    return tuple(sorted(codes, key=lambda code: str(code.value)))


def _codes(*entries: tuple[str | int, str, str]) -> tuple[Code, ...]:
    return tuple(Code(value, {"en": en, "es": es}) for value, en, es in entries)


FILE_FORMATS: tuple[tuple[str, str, str, str], ...] = (
    ("pdf", "application/pdf", "PDF", "PDF"),
    ("jpeg", "image/jpeg", "JPEG image", "Imagen JPEG"),
    ("png", "image/png", "PNG image", "Imagen PNG"),
    ("webp", "image/webp", "WebP image", "Imagen WebP"),
    ("heic", "image/heic", "HEIC image", "Imagen HEIC"),
    (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Word (.docx)",
        "Word (.docx)",
    ),
    (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Excel (.xlsx)",
        "Excel (.xlsx)",
    ),
    ("odt", "application/vnd.oasis.opendocument.text", "OpenDocument text", "Texto OpenDocument"),
    (
        "ods",
        "application/vnd.oasis.opendocument.spreadsheet",
        "OpenDocument sheet",
        "Hoja OpenDocument",
    ),
    ("txt", "text/plain", "Plain text", "Texto plano"),
    ("csv", "text/csv", "CSV", "CSV"),
    ("xml", "application/xml", "XML", "XML"),
    ("zip", "application/zip", "ZIP archive", "Archivo ZIP"),
)


@cache
def domains() -> dict[str, Domain]:
    """Every value domain, by id."""
    found = [
        Domain(
            "country",
            {"en": "Countries", "es": "Países"},
            "ISO 3166-1 alpha-2",
            _countries(),
        ),
        Domain(
            "language",
            {"en": "Languages", "es": "Idiomas"},
            "ISO 639-1",
            _languages(),
        ),
        Domain(
            "sex",
            {"en": "Sex", "es": "Sexo"},
            "ISO/IEC 5218",
            _codes(
                (0, "Not known", "Desconocido"),
                (1, "Male", "Hombre"),
                (2, "Female", "Mujer"),
                (9, "Not applicable", "No aplicable"),
            ),
        ),
        Domain(
            "identity_document_type",
            {"en": "Identity documents", "es": "Documentos de identidad"},
            "Almena",
            _codes(
                ("id_card", "National identity card", "Documento nacional de identidad"),
                ("passport", "Passport", "Pasaporte"),
                ("residence_permit", "Residence permit", "Permiso de residencia"),
                ("driving_licence", "Driving licence", "Permiso de conducir"),
            ),
        ),
        Domain(
            "education_level",
            {"en": "Education levels", "es": "Niveles educativos"},
            "UNESCO ISCED 2011",
            _codes(
                (0, "Early childhood education", "Educación de la primera infancia"),
                (1, "Primary education", "Educación primaria"),
                (2, "Lower secondary education", "Primera etapa de educación secundaria"),
                (3, "Upper secondary education", "Segunda etapa de educación secundaria"),
                (
                    4,
                    "Post-secondary non-tertiary education",
                    "Educación postsecundaria no terciaria",
                ),
                (5, "Short-cycle tertiary education", "Educación terciaria de ciclo corto"),
                (6, "Bachelor's or equivalent", "Grado o equivalente"),
                (7, "Master's or equivalent", "Máster o equivalente"),
                (8, "Doctoral or equivalent", "Doctorado o equivalente"),
            ),
        ),
        Domain(
            "file_format",
            {"en": "File formats", "es": "Formatos de fichero"},
            "IANA media types",
            tuple(
                Code(value, {"en": en, "es": es}, media_type=media)
                for value, media, en, es in FILE_FORMATS
            ),
        ),
    ]
    return {domain.id: domain for domain in found}


# Fields ----------------------------------------------------------------------

CATEGORIES: dict[str, Labels] = {
    "person": {"en": "Person", "es": "Persona"},
    "contact": {"en": "Contact", "es": "Contacto"},
    "document": {"en": "Documents", "es": "Documentos"},
    "education": {"en": "Education and work", "es": "Formación y trabajo"},
    "membership": {"en": "Membership", "es": "Afiliación"},
    "file": {"en": "Files", "es": "Ficheros"},
}

PID = "EU PID rule book (SD-JWT VC)"
OIDC = "OIDC Standard Claims"
MDOC = "ISO/IEC 18013-5, ISO/IEC 23220-2"


def _text(id: str, en: str, es: str, source: str, max_length: int = 100) -> Field:
    return Field(id, "text", {"en": en, "es": es}, source, max_length=max_length)


def _country(id: str, en: str, es: str, source: str) -> Field:
    return Field(id, "code", {"en": en, "es": es}, source, domain="country")


FIELDS: tuple[Field, ...] = (
    # Person
    Field(
        "given_name",
        "text",
        {"en": "Given name", "es": "Nombre"},
        f"{OIDC}, {PID}",
        "person",
        max_length=100,
    ),
    Field(
        "family_name",
        "text",
        {"en": "Family name", "es": "Apellidos"},
        f"{OIDC}, {PID}",
        "person",
        max_length=100,
    ),
    Field(
        "birthdate",
        "date",
        {"en": "Date of birth", "es": "Fecha de nacimiento"},
        f"{OIDC}, {PID}",
        "person",
    ),
    Field(
        "place_of_birth",
        "group",
        {"en": "Place of birth", "es": "Lugar de nacimiento"},
        PID,
        "person",
        parts=(
            Part("locality", _text("locality", "Locality", "Localidad", PID)),
            Part("region", _text("region", "Region", "Provincia o región", PID), required=False),
            Part("country", _country("country", "Country", "País", PID)),
        ),
    ),
    Field(
        "nationalities",
        "codes",
        {"en": "Nationalities", "es": "Nacionalidades"},
        PID,
        "person",
        domain="country",
    ),
    Field("sex", "code", {"en": "Sex", "es": "Sexo"}, PID, "person", domain="sex"),
    Field(
        "personal_administrative_number",
        "text",
        {"en": "Personal administrative number", "es": "Número de identificación personal"},
        PID,
        "person",
        max_length=50,
    ),
    Field(
        "tax_id",
        "text",
        {"en": "Tax identification number", "es": "Número de identificación fiscal"},
        "schema.org taxID",
        "person",
        max_length=50,
    ),
    Field(
        "preferred_language",
        "code",
        {"en": "Preferred language", "es": "Idioma preferido"},
        "ISO 639-1",
        "person",
        domain="language",
    ),
    # Contact
    Field("email", "email", {"en": "Email", "es": "Email"}, f"{OIDC}, {PID}", "contact"),
    Field(
        "phone_number",
        "phone",
        {"en": "Phone number", "es": "Teléfono"},
        f"{OIDC}, {PID}",
        "contact",
        pattern=E164,
    ),
    Field(
        "address",
        "group",
        {"en": "Address", "es": "Dirección"},
        f"{OIDC}, {PID}",
        "contact",
        parts=(
            Part("street_address", _text("street_address", "Street", "Calle", OIDC, 200)),
            Part(
                "house_number", _text("house_number", "Number", "Número", PID, 20), required=False
            ),
            Part("postal_code", _text("postal_code", "Postal code", "Código postal", OIDC, 20)),
            Part("locality", _text("locality", "Locality", "Localidad", OIDC)),
            Part("region", _text("region", "Region", "Provincia o región", OIDC), required=False),
            Part("country", _country("country", "Country", "País", OIDC)),
        ),
    ),
    # Documents
    Field(
        "identity_document",
        "group",
        {"en": "Identity document", "es": "Documento de identidad"},
        MDOC,
        "document",
        parts=(
            Part(
                "document_type",
                Field(
                    "document_type",
                    "code",
                    {"en": "Document type", "es": "Tipo de documento"},
                    "Almena",
                    domain="identity_document_type",
                ),
            ),
            Part(
                "document_number",
                _text("document_number", "Document number", "Número de documento", MDOC, 50),
            ),
            Part(
                "issuing_country",
                _country("issuing_country", "Issuing country", "País de expedición", MDOC),
            ),
            Part(
                "expiry_date",
                Field(
                    "expiry_date", "date", {"en": "Expiry date", "es": "Fecha de caducidad"}, MDOC
                ),
            ),
        ),
    ),
    Field(
        "portrait",
        "file",
        {"en": "Portrait photo", "es": "Fotografía"},
        MDOC,
        "document",
        domain="file_format",
        extra={"values": ("jpeg", "png")},
    ),
    # Education and work
    Field(
        "education_level",
        "code",
        {"en": "Education level", "es": "Nivel educativo"},
        "UNESCO ISCED 2011",
        "education",
        domain="education_level",
    ),
    Field(
        "organization_name",
        "text",
        {"en": "Organization", "es": "Organización"},
        "schema.org worksFor",
        "education",
        max_length=200,
    ),
    Field(
        "job_title",
        "text",
        {"en": "Job title", "es": "Puesto"},
        "schema.org jobTitle",
        "education",
        max_length=100,
    ),
    Field(
        "employee_number",
        "text",
        {"en": "Employee number", "es": "Número de empleado"},
        "schema.org identifier",
        "education",
        max_length=50,
    ),
    Field(
        "start_date",
        "date",
        {"en": "Start date", "es": "Fecha de inicio"},
        "schema.org startDate",
        "education",
    ),
    Field(
        "program_name",
        "text",
        {"en": "Programme", "es": "Estudios"},
        "EU Learning Model (ELM)",
        "education",
        max_length=200,
    ),
    Field(
        "academic_year",
        "text",
        {"en": "Academic year", "es": "Curso académico"},
        "Almena (YYYY-YYYY)",
        "education",
        max_length=9,
        pattern=r"^[0-9]{4}-[0-9]{4}$",
    ),
    Field(
        "degree_name",
        "text",
        {"en": "Qualification", "es": "Titulación"},
        "EU Learning Model (ELM)",
        "education",
        max_length=200,
    ),
    Field(
        "field_of_study",
        "text",
        {"en": "Field of study", "es": "Área de estudio"},
        "EU Learning Model (ELM)",
        "education",
        max_length=200,
    ),
    Field(
        "awarding_date",
        "date",
        {"en": "Date awarded", "es": "Fecha de expedición"},
        "EU Learning Model (ELM)",
        "education",
    ),
    # Membership
    Field(
        "member_number",
        "text",
        {"en": "Member number", "es": "Número de socio"},
        "schema.org membershipNumber",
        "membership",
        max_length=50,
    ),
    # Files
    Field(
        "document_file",
        "file",
        {"en": "Document", "es": "Documento"},
        "IANA media types",
        "file",
        domain="file_format",
        repeatable=True,
    ),
)

BY_ID: dict[str, Field] = {item.id: item for item in FIELDS}


def domain_values(item: Field) -> list[str | int]:
    """The values a coded or file field takes, before any form narrows them."""
    if item.codes:
        return [code.value for code in item.codes]
    assert item.domain is not None
    allowed = item.extra.get("values")
    return [
        code.value
        for code in domains()[item.domain].codes
        if allowed is None or code.value in allowed
    ]


def media_types(values: list[str | int]) -> list[str]:
    by_value = {code.value: code.media_type for code in domains()["file_format"].codes}
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
                    "media_type": {"enum": media_types(values or domain_values(item))},
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


def label_of(item: Field) -> str:
    """Its label in English, else in the first language it has."""
    return item.labels.get("en") or next(iter(item.labels.values()), item.id)


def field_schema(item: Field) -> dict[str, Any]:
    """The field's published JSON Schema document."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(item.id),
        "title": item.labels["en"],
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


@cache
def catalogue() -> dict[str, Any]:
    return {
        "version": VERSION,
        "languages": list(LANGUAGES),
        "categories": [{"id": key, "labels": labels} for key, labels in CATEGORIES.items()],
        "fields": [field_out(item) for item in FIELDS],
        "domains": {
            domain.id: {
                "labels": domain.labels,
                "source": domain.source,
                "codes": [_code_out(code) for code in domain.codes],
            }
            for domain in domains().values()
        },
    }
