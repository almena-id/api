"""A form's `credentials` block: the credentials it asks people to present.

Each entry names a type of Almena's credential catalogue (the trust
anchor's, `registry_api.trust_anchor`) and says, under a key of its own in the
form, whether it is required, why it is asked for (`purpose`, shown by the
wallet), which of the type's claims are wanted (selective disclosure; all of
them when none are named) and whom it is trusted from:

- `registry`: any issuer published in Almena's registry that grants the type;
- `issuers`: only those named, by DID — published issuers granting the type;
- `framework`: the type's own trust framework, for types issued elsewhere
  (the EU PID); the only mode they take, and one Almena's types never take.

A credential *fills* the form's fields named as its requested claims (the
fields and claims share the field catalogue's names): those come verified from
it, and are typed only when an optional credential is not presented.

The form's request travels as an OpenID4VP DCQL query (`dcql`): one credential
query per format the type has (SD-JWT VC, and W3C when it has a W3C type), the
alternatives of each entry in one credential set. mdoc is left out: its element
names (the PID's `birth_date`) are not the catalogue's, and would need mapping.
DCQL's `trusted_authorities` (key ids, ETSI trusted lists, OpenID Federation)
cannot express DIDs or Almena's registry, so trust is not put in the query:
it is the form's, checked when a presentation is verified.
"""

import re
import uuid
from typing import Any, Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import credential_catalog as catalog
from registry_api import texts
from registry_api.models import Identity, Issuer

KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TrustMode = Literal["registry", "issuers", "framework"]


class CredentialRequest(BaseModel):
    # Its name in the form; the type's id when none is given.
    key: str | None = Field(default=None, max_length=64)
    type: str = Field(max_length=64)
    required: bool = True
    # By language (`registry_api.texts`).
    purpose: dict[str, str] | None = None
    # Claims of the type to disclose; all of them when none are named.
    claims: list[str] | None = Field(default=None, max_length=50)
    trust: TrustMode | None = None
    # `issuers`: their DIDs.
    issuers: list[str] | None = Field(default=None, max_length=50)

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return (texts.clean(value, 300) or None) if value is not None else None


def _refuse(code: str, index: int) -> HTTPException:
    """A refusal about a credential request: which (its index in the list)."""
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": code, "credential": index}
    )


async def published_issuers(db: AsyncSession, type_id: str, tenant_id: uuid.UUID) -> set[str]:
    """The DIDs of the published issuers that grant the type: of any tenant,
    or — a tenant's own type (`custom:{key}`) — of that tenant alone."""
    query = (
        select(Identity.did, Issuer.credential_types)
        .join(Identity, Identity.id == Issuer.identity_id)
        .where(Issuer.published_at.is_not(None), Identity.did.is_not(None))
    )
    if type_id.startswith(catalog.PREFIX):
        query = query.where(Issuer.tenant_id == tenant_id)
    rows = await db.execute(query)
    return {did for did, types in rows if did and type_id in (types or [])}


Types = dict[str, catalog.CredentialType]


async def stored(
    db: AsyncSession, types: Types, requests: list[CredentialRequest], tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The block as kept, or why it cannot be; `types`, the catalogue as the
    form's tenant sees it."""
    kept: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        item = types.get(request.type)
        if item is None:
            raise _refuse("credential_unknown", index)
        key = request.key or item.key
        if not KEY.match(key):
            raise _refuse("credential_key_invalid", index)
        offered = [claim.name for claim in item.claims]
        # Left out, every claim of the type; sent, at least one.
        claims = offered if request.claims is None else request.claims
        if not claims or len(set(claims)) != len(claims) or set(claims) - set(offered):
            raise _refuse("credential_claims_invalid", index)
        external = item.issuance == "external"
        trust = request.trust or ("framework" if external else "registry")
        if (trust == "framework") != external:
            raise _refuse("credential_trust_invalid", index)
        entry: dict[str, Any] = {
            "key": key,
            "type": item.id,
            "required": request.required,
            # In the type's order.
            "claims": [claim for claim in offered if claim in claims],
            "trust": trust,
        }
        if request.purpose:
            entry["purpose"] = request.purpose
        if trust == "issuers":
            dids = request.issuers or []
            if (
                not dids
                or len(set(dids)) != len(dids)
                or set(dids) - await published_issuers(db, item.id, tenant_id)
            ):
                raise _refuse("credential_trust_invalid", index)
            entry["issuers"] = dids
        elif request.issuers:
            raise _refuse("credential_trust_invalid", index)
        kept.append(entry)
    for field, code in (("key", "credential_key_duplicate"), ("type", "credential_type_duplicate")):
        seen: set[str] = set()
        for index, entry in enumerate(kept):
            if entry[field] in seen:
                # The one that repeats what one before it took.
                raise _refuse(code, index)
            seen.add(entry[field])
    return kept


def fills(credentials: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, list[str]]:
    """For each credential, the keys of the form's fields it fills: fields,
    under their own name, that it asks for as claims (a claim is named as its
    field's key)."""
    named = {
        str(field["ref"]).removeprefix(catalog.PREFIX) for field in fields if "as" not in field
    }
    return {
        entry["key"]: [claim for claim in entry["claims"] if claim in named]
        for entry in credentials
    }


def _queries(types: Types, entry: dict[str, Any]) -> list[dict[str, Any]]:
    item = types[entry["type"]]
    claims = entry["claims"]
    found = [
        {
            "id": f"{entry['key']}_sd_jwt",
            "format": "dc+sd-jwt",
            "meta": {"vct_values": [catalog.vct(item)]},
            "claims": [{"path": [claim]} for claim in claims],
        }
    ]
    if item.w3c_type:
        found.append(
            {
                "id": f"{entry['key']}_w3c",
                "format": "jwt_vc_json",
                "meta": {"type_values": [["VerifiableCredential", item.w3c_type]]},
                "claims": [{"path": ["credentialSubject", claim]} for claim in claims],
            }
        )
    return found


def dcql(types: Types, credentials: list[dict[str, Any]]) -> dict[str, Any]:
    """The OpenID4VP DCQL query a form's credentials make: any one format of
    each type will do; the optional ones may be left out."""
    queries: list[dict[str, Any]] = []
    sets: list[dict[str, Any]] = []
    for entry in credentials:
        mine = _queries(types, entry)
        queries.extend(mine)
        sets.append({"options": [[query["id"]] for query in mine], "required": entry["required"]})
    return {"credentials": queries, "credential_sets": sets}
