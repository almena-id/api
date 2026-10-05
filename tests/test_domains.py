from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from registry_api import dns_proof
from registry_api.config import Settings
from tests.conftest import Dns, Outbox
from tests.test_directory import _sign_in


async def _add(
    client: AsyncClient, headers: dict[str, str], base: str, typed: str
) -> dict[str, Any]:
    response = await client.post(base, json={"domain": typed}, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_domain_is_added_proved_and_named_in_the_did(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    assert (await client.get(base, headers=headers)).json() == []

    added = await _add(client, headers, base, "https://Acme.com/about")
    assert added["domain"] == "acme.com" and added["verified"] is False
    record = added["dns_record"]
    assert record["name"] == "_almena.acme.com" and record["value"].startswith("almena-verify=")
    again = await client.post(base, json={"domain": "acme.com"}, headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "domain_exists"
    bad = await client.post(base, json={"domain": "not a domain"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["detail"] == "domain_invalid"

    # Not proved yet: nothing in the DID document.
    missing = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert missing.status_code == 422 and missing.json()["detail"] == "dns_record_not_found"
    own = (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["identity"]
    url = f"/api/v1/tenants/{tenant}/identities/{own['id']}"
    assert "service" not in (await client.get(url, headers=headers)).json()["document"]

    dns.records[record["name"]] = [record["value"]]
    checked = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert checked.status_code == 200 and checked.json()["verified"] is True
    # Checked again with the record still there: it stays verified.
    twice = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert twice.status_code == 200 and twice.json()["verified"] is True
    second = await _add(client, headers, base, "acme.org")
    dns.records[second["dns_record"]["name"]] = [second["dns_record"]["value"]]
    await client.post(f"{base}/{second['id']}/check", headers=headers)

    document = (await client.get(url, headers=headers)).json()["document"]
    assert "https://identity.foundation/.well-known/did-configuration/v1" in document["@context"]
    assert document["service"] == [
        {
            "id": f"{document['id']}#linked-domain",
            "type": "LinkedDomains",
            "serviceEndpoint": {"origins": ["https://acme.com", "https://acme.org"]},
        }
    ]

    removed = await client.delete(f"{base}/{second['id']}", headers=headers)
    assert removed.status_code == 204
    document = (await client.get(url, headers=headers)).json()["document"]
    assert document["service"][0]["serviceEndpoint"] == "https://acme.com"


async def test_only_admins_change_domains(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    added = await _add(client, ada, base, "acme.com")
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@acme.com", "role": "member"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@acme.com")
    # Members see them; only admins add, check or remove.
    assert [d["domain"] for d in (await client.get(base, headers=bob)).json()] == ["acme.com"]
    assert (await client.post(base, json={"domain": "x.com"}, headers=bob)).status_code == 403
    assert (await client.delete(f"{base}/{added['id']}", headers=bob)).status_code == 403
    # Nor anybody outside the tenant.
    eve, _ = await _sign_in(client, outbox, "eve@example.org")
    assert (await client.get(base, headers=eve)).status_code == 404


async def test_each_public_resolver_is_asked_and_any_one_seeing_the_record_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, str]] = []
    # The first still caches the record's absence; the second has it.
    seen = {"1.1.1.1": [], "8.8.8.8": ["v=spf1 -all", "almena-verify=abc"]}

    async def txt_at(nameserver: str, name: str) -> list[str] | None:
        asked.append((nameserver, name))
        return seen[nameserver]

    monkeypatch.setattr(dns_proof, "_txt_at", txt_at)
    records = await dns_proof.txt_records("_almena.acme.com")
    assert records == ["v=spf1 -all", "almena-verify=abc"]
    assert sorted(asked) == [("1.1.1.1", "_almena.acme.com"), ("8.8.8.8", "_almena.acme.com")]


async def test_dns_is_unavailable_only_when_no_public_resolver_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: dict[str, list[str] | None] = {"1.1.1.1": None, "8.8.8.8": []}

    async def txt_at(nameserver: str, name: str) -> list[str] | None:
        return answers[nameserver]

    monkeypatch.setattr(dns_proof, "_txt_at", txt_at)
    assert await dns_proof.txt_records("_almena.acme.com") == []
    answers["8.8.8.8"] = None
    with pytest.raises(dns_proof.DomainError) as raised:
        await dns_proof.txt_records("_almena.acme.com")
    assert raised.value.code == "dns_unavailable"


def test_the_public_resolvers_are_ip_addresses() -> None:
    assert [str(ip) for ip in Settings(dns_resolvers=["9.9.9.9", "2620:fe::fe"]).dns_resolvers] == [
        "9.9.9.9",
        "2620:fe::fe",
    ]
    for wrong in (["dns.google"], []):
        with pytest.raises(ValidationError):
            Settings(dns_resolvers=wrong)


async def test_a_verified_domain_whose_record_is_gone_stops_being_verified(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    added = await _add(client, headers, base, "acme.com")
    record = added["dns_record"]
    dns.records[record["name"]] = [record["value"]]
    assert (await client.post(f"{base}/{added['id']}/check", headers=headers)).json()["verified"]
    own = (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["identity"]
    url = f"/api/v1/tenants/{tenant}/identities/{own['id']}"
    assert "service" in (await client.get(url, headers=headers)).json()["document"]

    # Another value at the same name proves nothing.
    dns.records[record["name"]] = ["almena-verify=someone-else"]
    gone = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert gone.status_code == 422 and gone.json()["detail"] == "dns_record_not_found"
    listed = (await client.get(base, headers=headers)).json()
    assert listed[0]["verified"] is False and listed[0]["verified_at"] is None
    assert "service" not in (await client.get(url, headers=headers)).json()["document"]

    # Published again: verified again.
    dns.records[record["name"]] = ["almena-verify=someone-else", record["value"]]
    back = await client.post(f"{base}/{added['id']}/check", headers=headers)
    assert back.status_code == 200 and back.json()["verified"] is True


async def test_a_domain_linked_again_keeps_its_record(
    client: AsyncClient,
    outbox: Outbox,
    dns: Dns,
) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/domains"
    first = await _add(client, ada, base, "acme.com")
    dns.records[first["dns_record"]["name"]] = [first["dns_record"]["value"]]
    assert (await client.delete(f"{base}/{first['id']}", headers=ada)).status_code == 204

    # The record already published proves it again, at once.
    again = await _add(client, ada, base, "https://ACME.com/")
    assert again["dns_record"] == first["dns_record"]
    checked = await client.post(f"{base}/{again['id']}/check", headers=ada)
    assert checked.status_code == 200 and checked.json()["verified"] is True

    # Another tenant linking the same domain gets a record of its own.
    bob, other = await _sign_in(client, outbox, "bob@globex.com")
    theirs = await _add(client, bob, f"/api/v1/tenants/{other}/domains", "acme.com")
    assert theirs["dns_record"]["name"] == first["dns_record"]["name"]
    assert theirs["dns_record"]["value"] != first["dns_record"]["value"]
