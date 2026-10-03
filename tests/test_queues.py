from typing import Any

from httpx import AsyncClient

from registry_api.broker.memory import MemoryBroker
from tests.conftest import Outbox
from tests.test_directory import _sign_in
from tests.test_issuance import (
    CLAIMS,
    _accepted,
    answer_jws,
    issuer_id_of,
    read_sign_request,
    sign_status_list,
)
from tests.test_status_lists import _change


async def test_an_admin_makes_rotates_and_deletes_a_queue(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}"
    for kind in ("issuers", "verifiers"):
        item = (await client.post(f"{base}/{kind}", json={"name": "Club"}, headers=headers)).json()
        url = f"{base}/{kind}/{item['id']}/queue"
        before = (await client.get(url, headers=headers)).json()
        assert before["queue"] is None and before["created_at"] is None
        slug = before["user"]
        assert before["vhost"] == "almena" and before["amqp_url"].startswith("amqp")
        for missing in ("access", ""):
            path = f"{url}/{missing}".rstrip("/")
            response = await (
                client.post(path, headers=headers)
                if missing
                else client.delete(path, headers=headers)
            )
            assert response.status_code == 404 and response.json()["detail"] == "queue_not_found"

        made = await client.post(url, headers=headers)
        assert made.status_code == 201, made.text
        body = made.json()
        queue = f"subject.{slug}"
        assert body["queue"] == queue and body["user"] == slug and body["created_at"]
        # The broker has the queue, and a user that may read it alone.
        assert queue in broker.queues
        assert broker.users[slug] == (body["password"], queue)
        assert (await client.post(url, headers=headers)).json()["detail"] == "queue_exists"
        # Shown once: reading it again tells where, never the password.
        assert "password" not in (await client.get(url, headers=headers)).json()

        rotated = (await client.post(f"{url}/access", headers=headers)).json()
        assert rotated["password"] != body["password"]
        assert broker.users[slug] == (rotated["password"], queue)

        assert (await client.delete(url, headers=headers)).status_code == 204
        assert queue not in broker.queues and slug not in broker.users
        assert (await client.get(url, headers=headers)).json()["queue"] is None


async def test_only_admins_manage_a_queue_and_a_broker_down_makes_none(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}"
    item = (await client.post(f"{base}/issuers", json={"name": "Club"}, headers=headers)).json()
    url = f"{base}/issuers/{item['id']}/queue"
    await client.post(
        f"{base}/invitations", json={"email": "bob@acme.com", "role": "member"}, headers=headers
    )
    bob, _ = await _sign_in(client, outbox, "bob@acme.com")
    assert (await client.get(url, headers=bob)).status_code == 200
    refused = await client.post(url, headers=bob)
    assert refused.status_code == 403 and refused.json()["detail"] == "not_admin"
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(url, headers=eve)).status_code == 404

    broker.down = True
    down = await client.post(url, headers=headers)
    assert down.status_code == 503 and down.json()["detail"] == "broker_unavailable"
    assert (await client.get(url, headers=headers)).json()["queue"] is None


async def test_deleting_an_issuer_deletes_its_queue(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/issuers"
    item = (await client.post(base, json={"name": "Club"}, headers=headers)).json()
    made = (await client.post(f"{base}/{item['id']}/queue", headers=headers)).json()
    assert made["queue"] in broker.queues
    assert (await client.delete(f"{base}/{item['id']}", headers=headers)).status_code == 204
    assert made["queue"] not in broker.queues and made["user"] not in broker.users


async def test_the_issuers_queue_hears_what_happens_to_its_applications(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    queue: dict[str, str] = {}

    async def make_queue(headers: dict[str, str], tenant: str) -> None:
        issuer = await issuer_id_of(client, headers, tenant)
        made = await client.post(
            f"/api/v1/tenants/{tenant}/issuers/{issuer}/queue", headers=headers
        )
        assert made.status_code == 201, made.text
        queue["name"] = made.json()["queue"]

    club, headers, tenant, application_id, _, _ = await _accepted(client, outbox, make_queue)
    messages = broker.queues[queue["name"]]
    assert [kind for kind, _ in messages] == ["application.submitted", "application.decided"]
    submitted: dict[str, Any] = messages[0][1]
    assert submitted["type"] == "application.submitted"
    assert submitted["issuer"]["did"] == club.did
    assert submitted["application"]["id"] == application_id
    assert submitted["application"]["status"] == "submitted"
    assert submitted["digest"] and submitted["signature"] and submitted["content"]
    assert submitted["files"]["id_scan"]["url"].endswith(
        f"/tenants/{tenant}/applications/{application_id}/files/id_scan"
    )
    decided = messages[1][1]
    assert decided["application"]["status"] == "accepted" and "note" in decided

    # Issued, then suspended: each one more message.
    await sign_status_list(client, headers, tenant, club.wallet)
    issuing = f"/api/v1/tenants/{tenant}/applications/{application_id}/issuance"
    await client.put(issuing, json={"claims": CLAIMS, "valid_until": "2030-12-31"}, headers=headers)
    asked = await client.post(f"{issuing}/sign", json={"locale": "en"}, headers=headers)
    await answer_jws(client, await read_sign_request(client, asked.json()), club.wallet)
    suspended = await _change(client, headers, tenant, application_id, "suspended")
    signed = await answer_jws(
        client, await read_sign_request(client, suspended.json()), club.wallet
    )
    assert signed.status_code == 204, signed.text
    assert [kind for kind, _ in messages][2:] == ["application.issued", "credential.status"]
    assert messages[2][1]["valid_until"].startswith("2030-12-31")
    assert messages[3][1]["credential_status"] == "suspended"


async def test_a_broker_that_does_not_take_it_holds_nothing_up(
    client: AsyncClient, outbox: Outbox, broker: MemoryBroker
) -> None:
    async def make_queue(headers: dict[str, str], tenant: str) -> None:
        issuer = await issuer_id_of(client, headers, tenant)
        await client.post(f"/api/v1/tenants/{tenant}/issuers/{issuer}/queue", headers=headers)
        broker.down = True

    _, headers, tenant, application_id, _, _ = await _accepted(client, outbox, make_queue)
    inbox = f"/api/v1/tenants/{tenant}/applications/{application_id}"
    assert (await client.get(inbox, headers=headers)).json()["status"] == "accepted"
