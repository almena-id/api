import base64

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from registry_api import certification
from registry_api.main import app
from registry_api.root import create_root
from tests.conftest import Outbox
from tests.test_directory import _sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Dns:
    """TXT records by name, as the lookup finds them."""

    def __init__(self) -> None:
        self.records: dict[str, list[str]] = {}

    async def lookup(self, name: str) -> list[str]:
        return self.records.get(name, [])


@pytest.fixture
def dns() -> Dns:
    fake = Dns()
    app.dependency_overrides[certification.get_txt_lookup] = lambda: fake.lookup
    return fake


async def _ready(client: AsyncClient, headers: dict[str, str], tenant: str, dns: Dns) -> str:
    """A request filled in, its domain proved and its logo uploaded; its URL."""
    base = f"/api/v1/tenants/{tenant}/certification"
    body = {"legal_name": "Acme S.L.", "domain": "https://Acme.com/about"}
    state = (await client.put(f"{base}/request", json=body, headers=headers)).json()
    record = state["request"]["dns_record"]
    assert record["name"] == "_almena.acme.com"
    dns.records[record["name"]] = ['"other"', record["value"]]
    checked = await client.post(f"{base}/request/check-domain", headers=headers)
    assert checked.status_code == 200, checked.text
    assert checked.json()["request"]["domain_verified"] is True
    logo = await client.put(
        f"{base}/request/logo", content=PNG, headers=headers | {"Content-Type": "image/png"}
    )
    assert logo.status_code == 200, logo.text
    return base


async def _reviewer(client: AsyncClient, outbox: Outbox, db: AsyncSession) -> dict[str, str]:
    """A member of the root tenant, invited by `init-root`."""
    await create_root(db, "Almena", "reviewer@almena.id")
    headers, _ = await _sign_in(client, outbox, "reviewer@almena.id")
    return headers


async def test_a_request_is_filled_proved_and_sent(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/certification"
    assert (await client.get(base, headers=headers)).json() == {"current": None, "request": None}

    # Incomplete: nothing to send yet.
    await client.put(f"{base}/request", json={"legal_name": "Acme"}, headers=headers)
    incomplete = await client.post(f"{base}/request/submit", headers=headers)
    assert incomplete.status_code == 422 and incomplete.json()["detail"] == "request_incomplete"

    await _ready(client, headers, tenant, dns)
    state = (await client.get(base, headers=headers)).json()["request"]
    assert state["domain"] == "acme.com"
    assert state["logo"] == "data:image/png;base64," + base64.b64encode(PNG).decode()

    sent = await client.post(f"{base}/request/submit", headers=headers)
    assert sent.json()["request"]["status"] == "in_review"
    # Under review, it cannot change.
    locked = await client.put(f"{base}/request", json={"legal_name": "X"}, headers=headers)
    assert locked.status_code == 409 and locked.json()["detail"] == "in_review"


async def test_the_domain_needs_its_record(client: AsyncClient, outbox: Outbox, dns: Dns) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = f"/api/v1/tenants/{tenant}/certification"
    first = (
        await client.put(f"{base}/request", json={"domain": "acme.com"}, headers=headers)
    ).json()["request"]["dns_record"]
    missing = await client.post(f"{base}/request/check-domain", headers=headers)
    assert missing.status_code == 422 and missing.json()["detail"] == "dns_record_not_found"

    # Another domain, another token.
    other = (
        await client.put(f"{base}/request", json={"domain": "acme.org"}, headers=headers)
    ).json()["request"]["dns_record"]
    assert other["value"] != first["value"]


@pytest.mark.parametrize("typed", ["acme", "-acme.com", "acme..com", "127.0.0.1", " "])
async def test_a_domain_must_be_one(client: AsyncClient, outbox: Outbox, typed: str) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    response = await client.put(
        f"/api/v1/tenants/{tenant}/certification/request", json={"domain": typed}, headers=headers
    )
    assert response.status_code == 422 and response.json()["detail"] == "domain_invalid"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "logo_invalid"),
        (PNG * 5000, "logo_too_large"),
    ],
)
async def test_a_logo_must_be_a_small_image(
    client: AsyncClient, outbox: Outbox, data: bytes, code: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    response = await client.put(
        f"/api/v1/tenants/{tenant}/certification/request/logo",
        content=data,
        headers=headers | {"Content-Type": "image/png"},
    )
    assert response.status_code == 422 and response.json()["detail"] == code


async def test_only_admins_ask(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@acme.com")
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@acme.com", "role": "member"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@acme.com")
    base = f"/api/v1/tenants/{tenant}/certification"
    assert (await client.get(base, headers=bob)).status_code == 200
    denied = await client.put(f"{base}/request", json={"legal_name": "Mine"}, headers=bob)
    assert denied.status_code == 403


async def test_a_reviewer_approves_and_the_tenant_is_certified(
    client: AsyncClient, outbox: Outbox, dns: Dns, db: AsyncSession
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = await _ready(client, headers, tenant, dns)
    await client.post(f"{base}/request/submit", headers=headers)

    # Nobody reviews until the Almena tenant is set, and only its members.
    assert (await client.get("/api/v1/review/certifications", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["reviewer"] is False
    reviewer = await _reviewer(client, outbox, db)
    assert (await client.get("/api/v1/auth/me", headers=reviewer)).json()["reviewer"] is True

    queue = (await client.get("/api/v1/review/certifications", headers=reviewer)).json()
    assert [(r["legal_name"], r["domain"]) for r in queue] == [("Acme S.L.", "acme.com")]
    request_id = queue[0]["id"]
    detail = (
        await client.get(f"/api/v1/review/certifications/{request_id}", headers=reviewer)
    ).json()
    assert detail["tenant_name"] == "Tenant of ada@acme.com" and detail["current"] is None

    approved = await client.post(
        f"/api/v1/review/certifications/{request_id}/approve", headers=reviewer
    )
    assert approved.json()["status"] == "approved"
    state = (await client.get(base, headers=headers)).json()
    assert state["current"]["id"] == request_id and state["request"] is None
    tenants = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert tenants[0]["certified"] is True
    assert (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["certified"]

    # Its DID document names the domain it proved.
    identity = (await client.get(f"/api/v1/tenants/{tenant}", headers=headers)).json()["identity"]
    document = (
        await client.get(f"/api/v1/tenants/{tenant}/identities/{identity['id']}", headers=headers)
    ).json()["document"]
    assert document["@context"][1] == "https://identity.foundation/.well-known/did-configuration/v1"
    assert document["service"] == [
        {
            "id": f"{document['id']}#linked-domain",
            "type": "LinkedDomains",
            "serviceEndpoint": "https://acme.com",
        }
    ]

    # Its logo is public now.
    logo = await client.get(f"/api/v1/certifications/{request_id}/logo")
    assert logo.status_code == 200 and logo.content == PNG
    assert logo.headers["content-type"] == "image/png"


async def test_a_change_keeps_the_certification_until_it_is_decided(
    client: AsyncClient, outbox: Outbox, dns: Dns, db: AsyncSession
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = await _ready(client, headers, tenant, dns)
    await client.post(f"{base}/request/submit", headers=headers)
    reviewer = await _reviewer(client, outbox, db)
    first = (await client.get("/api/v1/review/certifications", headers=reviewer)).json()[0]["id"]
    await client.post(f"/api/v1/review/certifications/{first}/approve", headers=reviewer)

    # A new legal name opens a request from what is in force, domain proof included.
    changed = (
        await client.put(f"{base}/request", json={"legal_name": "Acme Group"}, headers=headers)
    ).json()
    assert changed["current"]["legal_name"] == "Acme S.L."
    assert changed["request"]["legal_name"] == "Acme Group"
    assert changed["request"]["domain_verified"] is True
    await client.post(f"{base}/request/submit", headers=headers)

    second = (await client.get("/api/v1/review/certifications", headers=reviewer)).json()[0]["id"]
    rejected = await client.post(
        f"/api/v1/review/certifications/{second}/reject",
        json={"reason": "  Not the registered name.  "},
        headers=reviewer,
    )
    assert rejected.json()["reason"] == "Not the registered name."
    state = (await client.get(base, headers=headers)).json()
    assert state["current"]["id"] == first
    assert state["request"]["status"] == "rejected"

    # Working on it again makes it a draft; approving it supersedes the first.
    again = await client.put(f"{base}/request", json={"legal_name": "Acme Group"}, headers=headers)
    assert again.json()["request"]["status"] == "draft"
    assert again.json()["request"]["reason"] is None
    await client.post(f"{base}/request/submit", headers=headers)
    await client.post(f"/api/v1/review/certifications/{second}/approve", headers=reviewer)
    state = (await client.get(base, headers=headers)).json()
    assert state["current"]["legal_name"] == "Acme Group"
    assert (await client.get(f"/api/v1/certifications/{first}/logo")).status_code == 404


async def test_a_decision_is_taken_once(
    client: AsyncClient, outbox: Outbox, dns: Dns, db: AsyncSession
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@acme.com")
    base = await _ready(client, headers, tenant, dns)
    await client.post(f"{base}/request/submit", headers=headers)
    reviewer = await _reviewer(client, outbox, db)
    request_id = (await client.get("/api/v1/review/certifications", headers=reviewer)).json()[0][
        "id"
    ]
    url = f"/api/v1/review/certifications/{request_id}"
    await client.post(f"{url}/approve", headers=reviewer)
    again = await client.post(f"{url}/reject", json={"reason": "x"}, headers=reviewer)
    assert again.status_code == 409 and again.json()["detail"] == "not_in_review"


def test_the_record_is_looked_for_under_the_label() -> None:
    assert certification.dns_record("acme.com", "t") == ("_almena.acme.com", "almena-verify=t")
    assert certification.domain("Bücher.example") == "xn--bcher-kva.example"
