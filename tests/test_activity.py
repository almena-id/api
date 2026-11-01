import os
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry_api.models import Application, Verification
from tests.conftest import Outbox
from tests.test_directory import _sign_in


async def test_the_activity_counts_each_day_and_where_things_stand(
    client: AsyncClient, outbox: Outbox, database: async_sessionmaker[AsyncSession]
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Club"}, headers=headers)).json()
    verifier = (
        await client.post(f"{base}/verifiers", json={"name": "Door"}, headers=headers)
    ).json()
    form = (
        await client.post(
            f"{base}/forms",
            json={"name": {"en": "Apply"}, "fields": [{"ref": "given_name"}]},
            headers=headers,
        )
    ).json()
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    long_ago = now - timedelta(days=60)

    def application(status: str, **when: datetime | str | None) -> Application:
        return Application(
            tenant_id=uuid.UUID(tenant),
            issuer_id=uuid.UUID(issuer["id"]),
            form_id=uuid.UUID(form["id"]),
            credential_type="membership",
            status=status,
            secret_hash=os.urandom(32),
            expires_at=now + timedelta(days=1),
            **when,
        )

    def verification(verified: bool, answered_at: datetime) -> Verification:
        return Verification(
            tenant_id=uuid.UUID(tenant),
            verifier_id=uuid.UUID(verifier["id"]),
            form_id=uuid.UUID(form["id"]),
            nonce=uuid.uuid4().hex,
            expires_at=answered_at + timedelta(minutes=5),
            answered_at=answered_at,
            verified=verified,
        )

    async with database() as db:
        db.add_all(
            [
                # Still being filled in: not counted.
                application("open"),
                application("submitted", submitted_at=now),
                application("rejected", submitted_at=yesterday, decided_at=now),
                application("accepted", submitted_at=yesterday, decided_at=yesterday),
                application("issued", submitted_at=yesterday, decided_at=yesterday, issued_at=now),
                application(
                    "issued",
                    submitted_at=long_ago,
                    decided_at=long_ago,
                    issued_at=long_ago,
                    credential_status="revoked",
                ),
                verification(True, now),
                verification(False, now),
                verification(True, long_ago),
            ]
        )
        await db.commit()

    response = await client.get(f"{base}/activity?days=7", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["days"]) == 7
    assert body["days"][-1] == {
        "date": now.date().isoformat(),
        "submitted": 1,
        "accepted": 0,
        "rejected": 1,
        "issued": 1,
        "verified": 1,
        "failed": 1,
    }
    assert body["days"][-2] == {
        "date": yesterday.date().isoformat(),
        "submitted": 3,
        "accepted": 2,
        "rejected": 0,
        "issued": 0,
        "verified": 0,
        "failed": 0,
    }
    assert body["applications"] == {"submitted": 1, "accepted": 1, "rejected": 1, "issued": 2}
    assert body["credentials"] == {"valid": 1, "suspended": 0, "revoked": 1}
    assert body["verifications"] == {"verified": 2, "failed": 1}

    for days in (6, 91):
        refused = await client.get(f"{base}/activity?days={days}", headers=headers)
        assert refused.status_code == 422
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    hidden = await client.get(f"{base}/activity", headers=eve)
    assert hidden.status_code == 404 and hidden.json()["detail"] == "tenant_not_found"
