"""Almena's catalogue of credential types: what issuers grant and forms ask for.

The types are data, the trust anchor's (`registry_api.trust_anchor`): it keeps
them for itself and every other tenant. This module is what a type is and how
it is published.

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
meaning; types may be added (by the anchor).
"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from registry_api import field_catalog as fields
from registry_api.config import get_settings

VERSION = "v1"
Labels = dict[str, str]


# How a tenant's own fields and types are referred to (`registry_api.trust_anchor`).
PREFIX = "custom:"


@dataclass(frozen=True)
class Claim:
    # A field of the catalogue, by its ref: one of the anchor's, or — in a
    # tenant's own type — one of the tenant's (`custom:{key}`).
    field: str
    required: bool = True

    @property
    def name(self) -> str:
        """Its name in a credential, and in forms: the field's key."""
        return self.field.removeprefix(PREFIX)


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
    # A tenant's own type (`custom:{key}`): the slug of the tenant, under
    # which it is published; none for the anchor's.
    namespace: str | None = None

    @property
    def key(self) -> str:
        return self.id.removeprefix(PREFIX)

    @property
    def path(self) -> str:
        """Where it lives under the identity domain: the anchor's by key, a
        tenant's under the tenant's slug."""
        return f"{self.namespace}/{self.key}" if self.namespace else self.key


def type_from(data: dict[str, Any], namespace: str | None = None) -> CredentialType:
    """A type as kept (`registry_api.models.CatalogType`, or its seed); with
    `namespace`, a tenant's own, referred to as `custom:{key}`."""
    return CredentialType(
        PREFIX + data["key"] if namespace else data["key"],
        data["labels"],
        data["descriptions"],
        data["category"],
        data["source"],
        tuple(Claim(claim["field"], claim.get("required", True)) for claim in data["claims"]),
        cast(Literal["almena", "external"], data.get("issuance") or "almena"),
        data.get("vct"),
        data.get("w3c_type"),
        data.get("mdoc_doctype"),
        namespace,
    )


def issuable(types: dict[str, CredentialType]) -> list[str]:
    """What a tenant's issuer may declare it grants, in the catalogue's order."""
    return [item.id for item in types.values() if item.issuance == "almena"]


def _base() -> str:
    return get_settings().did_url


def vct(item: CredentialType) -> str:
    return item.vct or f"{_base()}/credentials/{item.path}/{VERSION}"


def schema_url(item: CredentialType) -> str:
    return f"{_base()}/schemas/credentials/{VERSION}/{item.path}.json"


def metadata_url(item: CredentialType) -> str | None:
    """Where its SD-JWT VC Type Metadata is served: only for `vct`s of the
    identity domain."""
    if item.vct is not None:
        return None
    return f"{_base()}/.well-known/vct/credentials/{item.path}/{VERSION}"


def by_vct_path(types: dict[str, CredentialType], path: str) -> CredentialType | None:
    """The type whose `vct` is ``{did_url}/{path}``."""
    for item in types.values():
        if item.vct is None and path == f"credentials/{item.path}/{VERSION}":
            return item
    return None


def claims_schema(item: CredentialType, catalogue: dict[str, fields.Field]) -> dict[str, Any]:
    """The JSON Schema 2020-12 of its claims: each a published field's schema
    (`$ref`), a tenant's own field's written out whole."""

    def schema(claim: Claim) -> dict[str, Any]:
        field = catalogue.get(claim.field)
        if field is None or field.published:
            return {"$ref": fields.schema_url(claim.field)}
        return {"title": fields.label_of(field), **fields.value_schema(field)}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_url(item),
        "title": fields.label_of_texts(item.labels, item.key),
        "$comment": f"Almena credential catalogue {VERSION}; source: {item.source}"
        if item.source
        else f"A credential type of a tenant of Almena's registry, {VERSION}",
        "type": "object",
        "properties": {claim.name: schema(claim) for claim in item.claims},
        "required": [claim.name for claim in item.claims if claim.required],
    }


def _display(labels: Labels, key: str, more: Labels | None = None) -> list[dict[str, str]]:
    """Per language it has: a tenant's own may lack some."""
    shown = []
    for lang in fields.LANGUAGES:
        if lang not in labels:
            continue
        entry = {"locale": lang, key: labels[lang]}
        if more and lang in more:
            entry["description"] = more[lang]
        shown.append(entry)
    return shown


def type_metadata(item: CredentialType, catalogue: dict[str, fields.Field]) -> dict[str, Any]:
    """Its SD-JWT VC Type Metadata: names, claims with their labels, schema;
    `catalogue`, the fields its claims are."""
    claims: list[dict[str, Any]] = []
    for claim in item.claims:
        field = catalogue[claim.field]
        claims.append(
            {
                "path": [claim.name],
                "display": _display(field.labels, "label"),
                "mandatory": claim.required,
                "sd": "allowed",
            }
        )
        for part in field.parts:
            claims.append(
                {
                    "path": [claim.name, part.key],
                    "display": _display(part.field.labels, "label"),
                    "sd": "allowed",
                }
            )
    return {
        "vct": vct(item),
        "name": fields.label_of_texts(item.labels, item.key),
        "description": fields.label_of_texts(item.descriptions, ""),
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
        # Each a field of the catalogue by its ref, and the claim's name.
        "claims": [
            {"field": claim.field, "name": claim.name, "required": claim.required}
            for claim in item.claims
        ],
        "formats": formats,
        "schema": schema_url(item),
    }
    metadata = metadata_url(item)
    if metadata:
        out["metadata"] = metadata
    return out


def catalogue_out(types: list[CredentialType], categories: dict[str, Labels]) -> dict[str, Any]:
    """The catalogue as served: its types in order and their categories."""
    return {
        "version": VERSION,
        "languages": list(fields.LANGUAGES),
        "categories": [{"id": key, "labels": labels} for key, labels in categories.items()],
        "types": [type_out(item) for item in types],
    }
