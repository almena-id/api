"""The forms a tenant puts to people in its flows.

A form is what is asked of a person, whoever asks it: an issuer's offer puts
it to holders applying for a credential (checked by hand or with the issuer's
back office before it is granted), and a verifier's will put it to those it
verifies. It does not depend on which: it is its fields in order and the
credentials it asks to be presented. Each field is a field of Almena's
catalogue (`registry_api.field_catalog`), referred to by its id, and
the form only says whether it is required, adds a line of help and, where the
field's type allows it, makes it stricter (`narrow`): fewer values or file
formats, a date range, a shorter text. A field that may be asked for more than
once (a file) takes a name of its own in the form (`as`). Any member creates
them; each form's answers are described by a JSON Schema 2020-12 made from the
catalogue's (`…/schema`).

A form may also ask for credentials to be presented (`credentials`, see
`registry_api.form_credentials`): each fills the fields named as the claims it
asks for, and the whole request is an OpenID4VP DCQL query (`…/dcql`). A form
asks for one field or one credential at least.
"""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from registry_api import field_catalog as catalog
from registry_api import form_credentials, presentations, texts
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.custom_fields import PREFIX, custom_catalog
from registry_api.api.routes.directory import TenantId
from registry_api.models import Form

router = APIRouter(prefix="/tenants/{tenant_id}/forms", tags=["forms"])

# A form's own name for a repeatable field: lowercase, digits and underscores.
KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Narrow(BaseModel):
    """What a form may make stricter; which apply depends on the field's type."""

    model_config = ConfigDict(extra="forbid")

    # Coded fields and files: the values (file formats) it accepts, of its domain's.
    values: list[str | int] | None = Field(default=None, max_length=300)
    min_date: date | None = None
    max_date: date | None = None
    max_length: int | None = Field(default=None, ge=1)


class FormField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # A field of the catalogue, by its id.
    ref: str = Field(max_length=64)
    # Its name in this form, for a field asked for more than once.
    as_: str | None = Field(default=None, alias="as", max_length=64)
    required: bool = True
    # A line under the field telling people what is expected, by language.
    help: dict[str, str] | None = None
    narrow: Narrow | None = None

    @field_validator("help")
    @classmethod
    def _help(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return (texts.clean(value, 500) or None) if value is not None else None


class FormIn(BaseModel):
    # By language (`registry_api.texts`): one at least for the name.
    name: dict[str, str]
    description: dict[str, str] | None = None
    fields: list[FormField] = Field(default_factory=list, max_length=100)
    credentials: list[form_credentials.CredentialRequest] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: dict[str, str]) -> dict[str, str]:
        kept = texts.clean(value, 200)
        if not kept:
            raise ValueError("name is empty")
        return kept

    @field_validator("description")
    @classmethod
    def _description(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return (texts.clean(value, 2000) or None) if value is not None else None


class FormOut(BaseModel):
    id: uuid.UUID
    slug: str
    # By language.
    name: dict[str, str]
    description: dict[str, str] | None
    # Each field as `{ref, as?, required, help?, narrow?}`.
    fields: list[dict[str, Any]]
    # Each as `{key, type, required, purpose?, claims, trust, issuers?}`, with
    # `fills`: the keys of the fields it fills.
    credentials: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


def _refuse(code: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


def _narrow(item: catalog.Field, narrow: Narrow) -> dict[str, Any]:
    stored = narrow.model_dump(mode="json", exclude_none=True)
    if not stored:
        return {}
    if set(stored) - catalog.NARROWING.get(item.type, set()):
        raise _refuse("field_narrow_invalid")
    if narrow.values is not None:
        allowed = catalog.domain_values(item)
        # In the domain's order, whatever order they came in.
        values = [value for value in allowed if value in narrow.values]
        if not values or len(values) != len(narrow.values):
            raise _refuse("field_narrow_invalid")
        stored["values"] = values
    if narrow.min_date and narrow.max_date and narrow.min_date > narrow.max_date:
        raise _refuse("field_narrow_invalid")
    if narrow.max_length and item.max_length and narrow.max_length > item.max_length:
        raise _refuse("field_narrow_invalid")
    return stored


Fields = dict[str, catalog.Field]


def _key(ref: str) -> str:
    """A field's key in the answers: its id, or a custom field's own key."""
    return ref.removeprefix(PREFIX)


def _field(field: FormField, custom: Fields) -> tuple[str, dict[str, Any]]:
    """The field's key in the form and the field as stored, or why it cannot be."""
    item = catalog.BY_ID.get(field.ref) or custom.get(field.ref)
    if item is None:
        raise _refuse("field_unknown")
    if field.as_ is not None:
        if not item.repeatable:
            raise _refuse("field_rename_invalid")
        if not KEY.match(field.as_):
            raise _refuse("field_key_invalid")
    stored = field.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"narrow"})
    narrow = _narrow(item, field.narrow) if field.narrow else {}
    if narrow:
        stored["narrow"] = narrow
    return field.as_ or _key(field.ref), stored


def _fields(fields: list[FormField], custom: Fields) -> list[dict[str, Any]]:
    keyed = [_field(field, custom) for field in fields]
    if len({key for key, _ in keyed}) != len(keyed):
        raise _refuse("field_key_duplicate")
    return [stored for _, stored in keyed]


def form_schema(form: Form, custom: Fields) -> dict[str, Any]:
    """The JSON Schema 2020-12 a form's answers meet: each of Almena's fields is
    the catalogue's published schema (`$ref`), made stricter where the form
    narrows it; the tenant's own fields, unpublished, are written out whole."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for stored in form.fields:
        item = catalog.BY_ID.get(stored["ref"]) or custom[stored["ref"]]
        key = stored.get("as", _key(item.id))
        narrow = stored.get("narrow", {})
        schema: dict[str, Any]
        if item.published:
            schema = {"$ref": catalog.schema_url(item.id)}
        else:
            schema = {
                "title": catalog.label_of(item),
                **catalog.value_schema(item, narrow.get("values")),
            }
        if "help" in stored:
            schema["description"] = texts.text_of(stored["help"])
        # Unpublished fields took their values above, written out whole.
        if "values" in narrow and item.published:
            if item.type == "code":
                schema["enum"] = narrow["values"]
            elif item.type == "codes":
                schema["items"] = {"enum": narrow["values"]}
            else:
                schema["properties"] = {
                    "media_type": {"enum": catalog.media_types(narrow["values"])}
                }
        if "min_date" in narrow:
            schema["formatMinimum"] = narrow["min_date"]
        if "max_date" in narrow:
            schema["formatMaximum"] = narrow["max_date"]
        if "max_length" in narrow:
            schema["maxLength"] = narrow["max_length"]
        properties[key] = schema
        if stored.get("required", True):
            required.append(key)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": texts.text_of(form.name),
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


async def _owned(db: DbSession, tenant_id: uuid.UUID, form_id: uuid.UUID) -> Form:
    item = await db.get(Form, form_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="form_not_found")
    return item


def _out(item: Form) -> FormOut:
    filled = form_credentials.fills(item.credentials, item.fields)
    return FormOut(
        id=item.id,
        slug=item.slug,
        name=item.name,
        description=item.description,
        fields=item.fields,
        credentials=[{**entry, "fills": filled[entry["key"]]} for entry in item.credentials],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", summary="The tenant's forms, newest first")
async def list_forms(tenant_id: TenantId, db: DbSession) -> list[FormOut]:
    query = select(Form).where(Form.tenant_id == tenant_id)
    items = await db.scalars(query.order_by(Form.created_at.desc(), Form.id.desc()))
    return [_out(item) for item in items]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a form: its name, fields of the catalogue and credentials to present",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`fields_required` (no field nor credential), "
            "`field_unknown`, `field_rename_invalid`, `field_key_invalid`, "
            "`field_key_duplicate`, `field_narrow_invalid`, `credential_unknown`, "
            "`credential_key_invalid`, `credential_key_duplicate`, `credential_type_duplicate`, "
            "`credential_claims_invalid` or `credential_trust_invalid`"
        },
    },
)
async def create_form(tenant_id: TenantId, body: FormIn, db: DbSession) -> FormOut:
    if not body.fields and not body.credentials:
        raise _refuse("fields_required")
    item = Form(
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        fields=_fields(body.fields, await custom_catalog(db, tenant_id)),
        credentials=await form_credentials.stored(db, body.credentials),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _out(item)


@router.get(
    "/{form_id}",
    summary="One form",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`form_not_found`"}},
)
async def get_form(tenant_id: TenantId, form_id: uuid.UUID, db: DbSession) -> FormOut:
    return _out(await _owned(db, tenant_id, form_id))


@router.get(
    "/{form_id}/schema",
    summary="The JSON Schema its answers meet",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`form_not_found`"}},
)
async def get_form_schema(tenant_id: TenantId, form_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    form = await _owned(db, tenant_id, form_id)
    return form_schema(form, await custom_catalog(db, tenant_id))


@router.get(
    "/{form_id}/dcql",
    summary="The OpenID4VP DCQL query of the credentials it asks for",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`form_not_found`"}},
)
async def get_form_dcql(tenant_id: TenantId, form_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    form = await _owned(db, tenant_id, form_id)
    return form_credentials.dcql(form.credentials)


StatusFetch = Annotated[presentations.StatusFetch, Depends(presentations.get_status_fetch)]


class VerifyIn(BaseModel):
    # OpenID4VP's: for each DCQL credential query id, the presentations for it.
    vp_token: dict[str, list[str]] = Field(max_length=60)
    # What the holder's key binding must carry: the request's nonce, and the
    # verifier it was made for.
    nonce: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=500)


class Verified(BaseModel):
    key: str
    presented: bool
    verified: bool
    # The DCQL query it answered and its format.
    query: str | None
    format: str | None
    issuer: str | None
    # The claims the form asks for, as disclosed; only when verified.
    claims: dict[str, Any]
    # The form's fields it fills, with their values; only when verified.
    fills: dict[str, Any]
    problems: list[str]


class VerifyOut(BaseModel):
    # Every required credential presented and verified, and nothing presented
    # that failed.
    verified: bool
    credentials: list[Verified]


@router.post(
    "/{form_id}/verify",
    summary="Verify the credentials presented for a form (an OpenID4VP vp_token)",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`form_not_found`"}},
)
async def verify_presentations(
    tenant_id: TenantId, form_id: uuid.UUID, body: VerifyIn, db: DbSession, fetch: StatusFetch
) -> VerifyOut:
    form = await _owned(db, tenant_id, form_id)
    checked = await presentations.verify(
        db, form.credentials, body.vp_token, nonce=body.nonce, audience=body.audience, fetch=fetch
    )
    filled = form_credentials.fills(form.credentials, form.fields)
    required = {entry["key"] for entry in form.credentials if entry["required"]}
    results = [
        Verified(
            key=item.key,
            presented=item.presented,
            verified=item.verified,
            query=item.query,
            format=item.format,
            issuer=item.issuer,
            claims=item.claims,
            fills={key: item.claims[key] for key in filled[item.key] if key in item.claims}
            if item.verified
            else {},
            problems=item.problems
            if item.presented
            else (["not_presented"] if item.key in required else []),
        )
        for item in checked
    ]
    return VerifyOut(
        verified=all(
            item.verified if item.presented else item.key not in required for item in results
        ),
        credentials=results,
    )
