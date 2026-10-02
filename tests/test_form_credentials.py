from typing import Any

from httpx import AsyncClient

from tests.conftest import Outbox
from tests.signing import publish, ready, sign
from tests.test_directory import _sign_in


async def _published_issuer(
    client: AsyncClient, outbox: Outbox, email: str, types: list[str]
) -> str:
    """A published issuer of another tenant granting `types`; its DID."""
    headers, tenant = await _sign_in(client, outbox, email)
    base = f"/api/v1/tenants/{tenant}/issuers"
    created = (await client.post(base, json={"name": "Uni"}, headers=headers)).json()
    await client.put(
        f"{base}/{created['id']}/credential-types", json={"types": types}, headers=headers
    )
    wallet = await ready(client, headers, tenant)
    await sign(client, headers, tenant, created["identity"]["id"], wallet)
    await publish(client, headers, tenant, "issuers", created["id"], wallet)
    did: str = (await client.get(f"{base}/{created['id']}", headers=headers)).json()["did"]
    return did


async def test_a_form_asks_for_credentials_that_fill_its_fields(
    client: AsyncClient, outbox: Outbox
) -> None:
    university = await _published_issuer(client, outbox, "uni@example.edu", ["academic_degree"])
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"
    response = await client.post(
        base,
        json={
            "name": {"en": "Master's enrollment"},
            "fields": [
                {"ref": "given_name"},
                {"ref": "family_name"},
                {"ref": "birthdate"},
                {"ref": "address"},
            ],
            "credentials": [
                {
                    "key": "identity",
                    "type": "pid",
                    "required": False,
                    "purpose": {"en": " To check who you are ", "es": "Para saber quién eres"},
                    "claims": ["birthdate", "family_name", "given_name"],
                },
                {
                    "type": "academic_degree",
                    "claims": ["degree_name", "education_level"],
                    "trust": "issuers",
                    "issuers": [university],
                },
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    identity, degree = response.json()["credentials"]
    # Claims in the type's order; trust by the type's own framework.
    assert identity == {
        "key": "identity",
        "type": "pid",
        "required": False,
        "purpose": {"en": "To check who you are", "es": "Para saber quién eres"},
        "claims": ["given_name", "family_name", "birthdate"],
        "trust": "framework",
        "fills": ["given_name", "family_name", "birthdate"],
    }
    assert degree["key"] == "academic_degree" and degree["required"] is True
    assert degree["trust"] == "issuers" and degree["issuers"] == [university]
    assert degree["fills"] == []

    form_id = response.json()["id"]
    dcql = (await client.get(f"{base}/{form_id}/dcql", headers=headers)).json()
    queries = {query["id"]: query for query in dcql["credentials"]}
    assert set(queries) == {"identity_sd_jwt", "academic_degree_sd_jwt", "academic_degree_w3c"}
    assert queries["identity_sd_jwt"] == {
        "id": "identity_sd_jwt",
        "format": "dc+sd-jwt",
        "meta": {"vct_values": ["urn:eudi:pid:1"]},
        "claims": [{"path": ["given_name"]}, {"path": ["family_name"]}, {"path": ["birthdate"]}],
    }
    assert queries["academic_degree_w3c"]["meta"] == {
        "type_values": [["VerifiableCredential", "AcademicDegreeCredential"]]
    }
    assert queries["academic_degree_w3c"]["claims"][0] == {
        "path": ["credentialSubject", "degree_name"]
    }
    assert dcql["credential_sets"] == [
        {"options": [["identity_sd_jwt"]], "required": False},
        {"options": [["academic_degree_sd_jwt"], ["academic_degree_w3c"]], "required": True},
    ]

    # A form may ask only for credentials, every claim of the type by default.
    only = await client.post(
        base,
        json={
            "name": {"en": "Proof"},
            "credentials": [{"type": "membership"}],
        },
        headers=headers,
    )
    assert only.status_code == 201
    entry = only.json()["credentials"][0]
    assert entry["trust"] == "registry" and "member_number" in entry["claims"]


async def test_a_credential_request_must_hold(client: AsyncClient, outbox: Outbox) -> None:
    gym = await _published_issuer(client, outbox, "gym@example.org", ["membership"])
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/forms"

    async def refused(credentials: list[dict[str, Any]], fields: list[Any] | None = None) -> str:
        response = await client.post(
            base,
            json={
                "name": {"en": "X"},
                "fields": fields or [],
                "credentials": credentials,
            },
            headers=headers,
        )
        assert response.status_code == 422, response.text
        detail: str = response.json()["detail"]
        return detail

    assert await refused([]) == "fields_required"
    assert await refused([{"type": "passport"}]) == "credential_unknown"
    assert await refused([{"type": "pid", "key": "My ID"}]) == "credential_key_invalid"
    assert await refused([{"type": "pid"}, {"type": "pid"}]) == "credential_key_duplicate"
    assert await refused([{"type": "pid", "claims": ["member_number"]}]) == (
        "credential_claims_invalid"
    )
    assert await refused([{"type": "pid", "claims": ["email", "email"]}]) == (
        "credential_claims_invalid"
    )
    # Sent empty is none at all, not every claim.
    assert await refused([{"type": "pid", "claims": []}]) == "credential_claims_invalid"
    # A type is asked for once, whatever its key.
    assert await refused([{"type": "pid"}, {"type": "pid", "key": "pid2"}]) == (
        "credential_type_duplicate"
    )
    # The PID is trusted by its framework only; Almena's types never by it.
    assert await refused([{"type": "pid", "trust": "registry"}]) == "credential_trust_invalid"
    assert await refused([{"type": "membership", "trust": "framework"}]) == (
        "credential_trust_invalid"
    )
    # Named issuers are published ones that grant the type.
    assert await refused([{"type": "membership", "trust": "issuers"}]) == (
        "credential_trust_invalid"
    )
    assert await refused(
        [{"type": "membership", "trust": "issuers", "issuers": ["did:web:nowhere"]}]
    ) == ("credential_trust_invalid")
    assert await refused([{"type": "employment", "trust": "issuers", "issuers": [gym]}]) == (
        "credential_trust_invalid"
    )
    assert await refused([{"type": "membership", "issuers": [gym]}]) == ("credential_trust_invalid")
