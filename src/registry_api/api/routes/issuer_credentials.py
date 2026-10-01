"""The credential types an issuer grants, from Almena's catalogue, and the
form a holder fills in to ask for each — together, its offers.

Any member declares them, as with the rest of an issuer's data; only types a
tenant may issue are taken (not the EU PID, issued by member states), and only
the tenant's own forms. A type with a form is *offered*:
the public catalogue lists it, and holders apply for it there
(`registry_api.api.routes.applications`).
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from registry_api import credential_catalog as catalog
from registry_api.api.routes.auth import DbSession
from registry_api.api.routes.directory import TenantId
from registry_api.models import Form, Issuer

router = APIRouter(
    prefix="/tenants/{tenant_id}/issuers/{issuer_id}/credential-types", tags=["directory"]
)


class CredentialTypes(BaseModel):
    # Ids of the credential type catalogue, in its order.
    types: list[str] = Field(max_length=len(catalog.TYPES))
    # For each type, the form holders fill in to ask for it.
    forms: dict[str, uuid.UUID] = Field(default_factory=dict)


async def _issuer(db: DbSession, tenant_id: uuid.UUID, issuer_id: uuid.UUID) -> Issuer:
    item = await db.get(Issuer, issuer_id)
    if item is None or item.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="issuer_not_found")
    return item


def _out(item: Issuer) -> CredentialTypes:
    return CredentialTypes(
        types=item.credential_types,
        forms={key: uuid.UUID(value) for key, value in (item.request_forms or {}).items()},
    )


@router.get(
    "",
    summary="The credential types the issuer grants, and the form for each",
    responses={status.HTTP_404_NOT_FOUND: {"description": "`issuer_not_found`"}},
)
async def get_types(tenant_id: TenantId, issuer_id: uuid.UUID, db: DbSession) -> CredentialTypes:
    return _out(await _issuer(db, tenant_id, issuer_id))


@router.put(
    "",
    summary="Declare the credential types the issuer grants, and their forms (any member)",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "`issuer_not_found`"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`credential_type_invalid`: not in the catalogue, or not one a "
            "tenant issues; `request_form_invalid`: not one of the tenant's "
            "forms, or for a type it does not grant"
        },
    },
)
async def set_types(
    tenant_id: TenantId, issuer_id: uuid.UUID, body: CredentialTypes, db: DbSession
) -> CredentialTypes:
    item = await _issuer(db, tenant_id, issuer_id)
    if set(body.types) - set(catalog.ISSUABLE):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="credential_type_invalid")
    if set(body.forms) - set(body.types):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="request_form_invalid")
    if body.forms:
        owned = set(
            await db.scalars(
                select(Form.id).where(
                    Form.tenant_id == tenant_id,
                    Form.id.in_(body.forms.values()),
                )
            )
        )
        if set(body.forms.values()) - owned:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail="request_form_invalid"
            )
    item.credential_types = [type_id for type_id in catalog.ISSUABLE if type_id in body.types]
    item.request_forms = {key: str(value) for key, value in body.forms.items()}
    await db.commit()
    return _out(item)
