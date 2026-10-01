"""Almena's catalogue of credential types: what issuers grant and forms ask for.

Each type names its claims as fields of the field catalogue
(`registry_api.field_catalog`), so the same name means the same thing in what
a holder types, what a credential carries and what a form asks to be
presented — a form can tell by itself which of its fields a credential fills.

A type is identified the way each format identifies it: an SD-JWT VC `vct`, a
W3C VCDM 2.0 `type`, an mdoc `doctype`. Almena's own types live under the
identity domain: their `vct` is ``{did_url}/credentials/{id}/v1``, whose SD-JWT
VC Type Metadata is served at ``{did_url}/.well-known/vct/credentials/{id}/v1``
(the type metadata rule for https `vct`s); the JSON Schema of their claims, at
``{did_url}/schemas/credentials/v1/{id}.json``, is the `schema_uri` of that
metadata and a W3C credential's `credentialSchema`.

Some types are issued under a framework of their own — the EU PID, by member
states — and listed so forms can ask for them; no tenant issues them
(`issuance: external`).

Versioned like the field catalogue: a published version never changes
meaning; types may be added.
"""

from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

from registry_api import field_catalog as fields
from registry_api.config import get_settings

VERSION = "v1"
Labels = dict[str, str]


@dataclass(frozen=True)
class Claim:
    # A field of the field catalogue; its id is the claim's name.
    field: str
    required: bool = True


@dataclass(frozen=True)
class CredentialType:
    id: str
    labels: Labels
    descriptions: Labels
    category: str
    source: str
    claims: tuple[Claim, ...]
    # `almena`: any tenant's issuer may grant it; `external`: issued under a
    # framework of its own, only asked for.
    issuance: Literal["almena", "external"] = "almena"
    # How each format names it; Almena's own `vct` is derived from the id.
    vct: str | None = None
    w3c_type: str | None = None
    mdoc_doctype: str | None = None


CATEGORIES: dict[str, Labels] = {
    "identity": {"en": "Identity", "es": "Identidad"},
    "contact": {"en": "Contact", "es": "Contacto"},
    "education": {"en": "Education", "es": "Formación"},
    "work": {"en": "Work", "es": "Trabajo"},
    "membership": {"en": "Membership", "es": "Afiliación"},
}

NAME = (Claim("given_name"), Claim("family_name"))

TYPES: tuple[CredentialType, ...] = (
    CredentialType(
        "pid",
        {"en": "Person identification data (EU PID)", "es": "Datos de identificación (PID UE)"},
        {
            "en": "The EU Digital Identity Wallet's identity credential, issued by member states.",
            "es": "La credencial de identidad de la Cartera Europea de Identidad Digital, "
            "expedida por los Estados miembros.",
        },
        "identity",
        "EU PID rule book (ARF)",
        (
            *NAME,
            Claim("birthdate"),
            Claim("place_of_birth", required=False),
            Claim("nationalities"),
            Claim("sex", required=False),
            Claim("address", required=False),
            Claim("personal_administrative_number", required=False),
            Claim("email", required=False),
            Claim("phone_number", required=False),
        ),
        issuance="external",
        vct="urn:eudi:pid:1",
        mdoc_doctype="eu.europa.ec.eudi.pid.1",
    ),
    CredentialType(
        "residence",
        {"en": "Proof of residence", "es": "Certificado de residencia"},
        {
            "en": "Where a person lives, as the issuer has checked it.",
            "es": "Dónde reside una persona, tal como lo ha comprobado el emisor.",
        },
        "identity",
        "Almena",
        (*NAME, Claim("address"), Claim("personal_administrative_number", required=False)),
        w3c_type="ResidenceCredential",
    ),
    CredentialType(
        "verified_email",
        {"en": "Verified email", "es": "Email verificado"},
        {
            "en": "An email address its holder has proved to control.",
            "es": "Una dirección de email que su titular ha demostrado controlar.",
        },
        "contact",
        "Almena",
        (Claim("email"),),
        w3c_type="VerifiedEmailCredential",
    ),
    CredentialType(
        "verified_phone",
        {"en": "Verified phone number", "es": "Teléfono verificado"},
        {
            "en": "A phone number its holder has proved to control.",
            "es": "Un número de teléfono que su titular ha demostrado controlar.",
        },
        "contact",
        "Almena",
        (Claim("phone_number"),),
        w3c_type="VerifiedPhoneCredential",
    ),
    CredentialType(
        "enrollment",
        {"en": "Student enrollment", "es": "Matrícula de estudiante"},
        {
            "en": "That a person is enrolled in a programme for an academic year.",
            "es": "Que una persona está matriculada en unos estudios durante un curso.",
        },
        "education",
        "EU Learning Model (ELM), simplified",
        (
            *NAME,
            Claim("organization_name"),
            Claim("program_name"),
            Claim("academic_year"),
        ),
        w3c_type="EnrollmentCredential",
    ),
    CredentialType(
        "academic_degree",
        {"en": "Academic qualification", "es": "Título académico"},
        {
            "en": "A qualification awarded to a person, with its level and awarding body.",
            "es": "Una titulación otorgada a una persona, con su nivel y quién la otorga.",
        },
        "education",
        "EU Learning Model (ELM), simplified; UNESCO ISCED 2011",
        (
            *NAME,
            Claim("birthdate", required=False),
            Claim("degree_name"),
            Claim("field_of_study", required=False),
            Claim("education_level"),
            Claim("organization_name"),
            Claim("awarding_date"),
        ),
        w3c_type="AcademicDegreeCredential",
    ),
    CredentialType(
        "employment",
        {"en": "Employment", "es": "Relación laboral"},
        {
            "en": "That a person works for an organization, in which role and since when.",
            "es": "Que una persona trabaja para una organización, en qué puesto y desde cuándo.",
        },
        "work",
        "schema.org EmployeeRole, simplified",
        (
            *NAME,
            Claim("organization_name"),
            Claim("job_title"),
            Claim("employee_number", required=False),
            Claim("start_date"),
        ),
        w3c_type="EmploymentCredential",
    ),
    CredentialType(
        "membership",
        {"en": "Membership", "es": "Afiliación"},
        {
            "en": "That a person is a member of an organization: a club, an association, "
            "a professional body.",
            "es": "Que una persona es miembro de una organización: un club, una asociación, "
            "un colegio profesional.",
        },
        "membership",
        "schema.org ProgramMembership, simplified",
        (
            *NAME,
            Claim("organization_name"),
            Claim("member_number"),
            Claim("start_date", required=False),
        ),
        w3c_type="MembershipCredential",
    ),
)

BY_ID: dict[str, CredentialType] = {item.id: item for item in TYPES}
# What a tenant's issuer may declare it grants.
ISSUABLE = tuple(item.id for item in TYPES if item.issuance == "almena")


def _base() -> str:
    return get_settings().did_url


def vct(item: CredentialType) -> str:
    return item.vct or f"{_base()}/credentials/{item.id}/{VERSION}"


def schema_url(item: CredentialType) -> str:
    return f"{_base()}/schemas/credentials/{VERSION}/{item.id}.json"


def metadata_url(item: CredentialType) -> str | None:
    """Where its SD-JWT VC Type Metadata is served: only for Almena's `vct`s."""
    if item.vct is not None:
        return None
    return f"{_base()}/.well-known/vct/credentials/{item.id}/{VERSION}"


def by_vct_path(path: str) -> CredentialType | None:
    """The type whose `vct` is ``{did_url}/{path}``."""
    for item in TYPES:
        if item.vct is None and path == f"credentials/{item.id}/{VERSION}":
            return item
    return None


def claims_schema(item: CredentialType) -> dict[str, Any]:
    """The JSON Schema 2020-12 of its claims: each the field catalogue's schema."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(item),
        "title": item.labels["en"],
        "$comment": f"Almena credential catalogue {VERSION}; source: {item.source}",
        "type": "object",
        "properties": {
            claim.field: {"$ref": fields.schema_url(claim.field)} for claim in item.claims
        },
        "required": [claim.field for claim in item.claims if claim.required],
    }


def _display(labels: Labels, key: str, more: Labels | None = None) -> list[dict[str, str]]:
    shown = []
    for lang in fields.LANGUAGES:
        entry = {"locale": lang, key: labels[lang]}
        if more:
            entry["description"] = more[lang]
        shown.append(entry)
    return shown


def type_metadata(item: CredentialType) -> dict[str, Any]:
    """Its SD-JWT VC Type Metadata: names, claims with their labels, schema."""
    claims: list[dict[str, Any]] = []
    for claim in item.claims:
        field = fields.BY_ID[claim.field]
        claims.append(
            {
                "path": [field.id],
                "display": _display(field.labels, "label"),
                "mandatory": claim.required,
                "sd": "allowed",
            }
        )
        for part in field.parts:
            claims.append(
                {
                    "path": [field.id, part.key],
                    "display": _display(part.field.labels, "label"),
                    "sd": "allowed",
                }
            )
    return {
        "vct": vct(item),
        "name": item.labels["en"],
        "description": item.descriptions["en"],
        "display": _display(item.labels, "name", item.descriptions),
        "claims": claims,
        "schema_uri": schema_url(item),
    }


def type_out(item: CredentialType) -> dict[str, Any]:
    formats: dict[str, Any] = {"dc+sd-jwt": {"vct": vct(item)}}
    if item.w3c_type:
        formats["jwt_vc_json"] = {"type": ["VerifiableCredential", item.w3c_type]}
    if item.mdoc_doctype:
        formats["mso_mdoc"] = {"doctype": item.mdoc_doctype}
    out: dict[str, Any] = {
        "id": item.id,
        "category": item.category,
        "labels": item.labels,
        "descriptions": item.descriptions,
        "source": item.source,
        "issuance": item.issuance,
        "claims": [{"field": claim.field, "required": claim.required} for claim in item.claims],
        "formats": formats,
        "schema": schema_url(item),
    }
    metadata = metadata_url(item)
    if metadata:
        out["metadata"] = metadata
    return out


@cache
def catalogue() -> dict[str, Any]:
    return {
        "version": VERSION,
        "languages": list(fields.LANGUAGES),
        "categories": [{"id": key, "labels": labels} for key, labels in CATEGORIES.items()],
        "types": [type_out(item) for item in TYPES],
    }
