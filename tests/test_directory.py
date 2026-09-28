import uuid
from urllib.parse import quote, urlsplit

import pytest
from httpx import AsyncClient

from registry_api.config import get_settings
from tests.conftest import Outbox


async def _sign_in(client: AsyncClient, outbox: Outbox, email: str) -> tuple[dict[str, str], str]:
    """Headers for a new account, and the id of the tenant it was created with."""
    await client.post("/api/v1/auth/code", json={"email": email})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": email, "code": outbox.last_code()}
    )
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    tenant = (await client.get("/api/v1/tenants", headers=headers)).json()[0]["id"]
    return headers, tenant


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_create_then_list(client: AsyncClient, outbox: Outbox, kind: str) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/{kind}"

    empty = (await client.get(base, headers=headers)).json()
    assert empty == {"items": [], "next_cursor": None, "total": 0}

    body = {"name": "  Acme  ", "description": "  "}
    created = await client.post(base, json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Acme"
    assert created.json()["description"] is None

    listed = (await client.get(base, headers=headers)).json()
    assert [i["name"] for i in listed["items"]] == ["Acme"]
    assert listed["total"] == 1


async def test_descriptions_are_kept(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = {"name": "Uni", "description": "Degrees"}
    created = await client.post(f"/api/v1/tenants/{tenant}/issuers", json=body, headers=headers)
    assert created.json()["description"] == "Degrees"


async def test_a_name_is_required(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    for name in ["", "   ", "x" * 201]:
        response = await client.post(
            f"/api/v1/tenants/{tenant}/verifiers", json={"name": name}, headers=headers
        )
        assert response.status_code == 422


async def test_pages_follow_the_cursor_to_the_end(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}/verifiers"
    for n in range(7):
        await client.post(base, json={"name": f"id-{n}"}, headers=headers)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while pages < 10:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(base, params=params, headers=headers)).json()
        assert page["total"] == 7
        seen += [i["name"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert sorted(seen) == [f"id-{n}" for n in range(7)]
    assert len(set(seen)) == 7


async def test_a_bad_cursor_is_refused(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    response = await client.get(
        f"/api/v1/tenants/{tenant}/issuers", params={"cursor": "nonsense"}, headers=headers
    )
    assert response.status_code == 400


async def test_another_tenant_is_out_of_reach(client: AsyncClient, outbox: Outbox) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    base = f"/api/v1/tenants/{ada_tenant}/issuers"
    await client.post(base, json={"name": "Ada's"}, headers=ada)

    assert (await client.get(base, headers=bob)).status_code == 404
    assert (await client.post(base, json={"name": "Bob's"}, headers=bob)).status_code == 404
    assert (await client.get(base)).status_code == 401


async def test_an_issuer_gets_an_identity_of_its_own(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    assert issuer["identity"]["name"] == "Uni"

    identities = (await client.get(f"{base}/identities", headers=headers)).json()
    # The tenant's own, and the issuer's (newest first).
    assert identities["total"] == 2
    newest = identities["items"][0]
    assert newest["id"] == issuer["identity"]["id"]
    assert newest["used_by"] == [{"kind": "issuer", "id": issuer["id"], "name": "Uni"}]


async def test_issuers_and_verifiers_never_share_an_identity(
    client: AsyncClient, outbox: Outbox
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    # An identity to act as is not something that can be chosen any more.
    body = {"name": "Acme desk", "identity_id": own}
    issuer = (await client.post(f"{base}/issuers", json=body, headers=headers)).json()
    verifier = (await client.post(f"{base}/verifiers", json=body, headers=headers)).json()
    assert len({own, issuer["identity"]["id"], verifier["identity"]["id"]}) == 3

    identities = (await client.get(f"{base}/identities", headers=headers)).json()
    assert identities["total"] == 3


async def test_an_identity_shows_its_did_document(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()

    response = await client.get(f"{base}/identities/{issuer['identity']['id']}", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["name"] == "Uni"
    assert detail["used_by"] == [{"kind": "issuer", "id": issuer["id"], "name": "Uni"}]
    did = detail["did"]
    # Made from the identity domain, not the API's origin.
    host = urlsplit(get_settings().did_url).netloc
    assert did.startswith(f"did:web:{quote(host, safe='')}:ids:idn_")
    # Its tenant controls it; no mediator yet: nothing to route messages through.
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    tenant_did = (await client.get(f"{base}/identities/{own}", headers=headers)).json()["did"]
    assert detail["document"] == {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "controller": tenant_did,
    }

    # The same document, public once published, where did:web resolves it.
    await client.post(f"{base}/issuers/{issuer['id']}/publish", headers=headers)
    slug = did.rsplit(":", 1)[1]
    assert detail["document_url"] == f"{get_settings().did_url}/ids/{slug}/did.json"
    published = await client.get(f"/ids/{slug}/did.json")
    assert published.status_code == 200
    assert published.headers["content-type"] == "application/did+json"
    assert published.json() == detail["document"]


async def test_the_did_document_names_the_mediator(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{base}/mediators", json=body, headers=headers)).json()
    await client.patch(base, json={"mediator_id": mediator["id"]}, headers=headers)
    # Nothing routes through a draft mediator.
    await client.post(f"{base}/mediators/{mediator['id']}/publish", headers=headers)
    mediator_did = (await client.get(f"{base}/mediators/{mediator['id']}", headers=headers)).json()[
        "did"
    ]
    identity_id = (await client.get(base, headers=headers)).json()["identity"]["id"]

    detail = (await client.get(f"{base}/identities/{identity_id}", headers=headers)).json()
    assert detail["used_by"][0]["kind"] == "tenant"
    assert detail["document"]["service"] == [
        {
            "id": f"{detail['did']}#didcomm",
            "type": "DIDCommMessaging",
            "serviceEndpoint": {"uri": mediator_did, "accept": ["didcomm/v2"]},
        }
    ]


async def test_identities_stay_in_their_tenant(client: AsyncClient, outbox: Outbox) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, bob_tenant = await _sign_in(client, outbox, "bob@example.org")
    own = (await client.get(f"/api/v1/tenants/{ada_tenant}", headers=ada)).json()["identity"]["id"]

    for path in (f"{bob_tenant}/identities/{own}", f"{ada_tenant}/identities/{own}"):
        response = await client.get(f"/api/v1/tenants/{path}", headers=bob)
        assert response.status_code == 404
    assert (await client.get("/ids/idn_nothere/did.json")).status_code == 404


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_one_opens_and_changes(client: AsyncClient, outbox: Outbox, kind: str) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    body = {"name": "Relay", "url": "https://mediator.example.org"}
    mediator = (await client.post(f"{base}/mediators", json=body, headers=headers)).json()
    created = (await client.post(f"{base}/{kind}", json={"name": "Uni"}, headers=headers)).json()
    url = f"{base}/{kind}/{created['id']}"

    detail = (await client.get(url, headers=headers)).json()
    assert detail["name"] == "Uni" and detail["mediator"] is None
    assert detail["did"].split(":")[-2:][0] == "ids"

    patch = {"name": " Uni 2 ", "description": "Degrees", "mediator_id": mediator["id"]}
    changed = await client.patch(url, json=patch, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Uni 2"
    assert changed.json()["description"] == "Degrees"
    assert changed.json()["mediator"] == {"id": mediator["id"], "name": "Relay"}
    # Its DID stays; its identity follows the name.
    assert changed.json()["did"] == detail["did"]
    assert changed.json()["identity"]["name"] == "Uni 2"

    # Only what is sent changes; null removes.
    cleared = (
        await client.patch(url, json={"description": None, "mediator_id": None}, headers=headers)
    ).json()
    assert cleared["name"] == "Uni 2"
    assert cleared["description"] is None and cleared["mediator"] is None

    blank = await client.patch(url, json={"name": "  "}, headers=headers)
    assert blank.status_code == 422 and blank.json()["detail"] == "name_required"
    other = await client.patch(url, json={"mediator_id": str(uuid.uuid4())}, headers=headers)
    assert other.status_code == 422 and other.json()["detail"] == "mediator_not_found"

    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    assert (await client.get(url, headers=bob)).status_code == 404
    missing = await client.get(f"{base}/{kind}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
