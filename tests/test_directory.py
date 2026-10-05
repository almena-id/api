import json
import uuid
from urllib.parse import quote, urlsplit

import pytest
from httpx import AsyncClient

from registry_api.config import get_settings
from tests.conftest import Dns, Outbox
from tests.fake_wallet import FakeWallet
from tests.mediators import new_mediator
from tests.signing import link_wallet, publish, ready, sign


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

    body = {"name": "  Acme  ", "description": {"en": "  "}}
    created = await client.post(base, json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Acme"
    assert created.json()["description"] is None

    listed = (await client.get(base, headers=headers)).json()
    assert [i["name"] for i in listed["items"]] == ["Acme"]
    assert listed["total"] == 1
    # Each row says where its DID's signature stands: nothing signed yet.
    assert listed["items"][0]["signature"] == "pending"


async def test_descriptions_are_kept(client: AsyncClient, outbox: Outbox) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    body = {"name": "Uni", "description": {"en": "Degrees"}}
    created = await client.post(f"/api/v1/tenants/{tenant}/issuers", json=body, headers=headers)
    assert created.json()["description"] == {"en": "Degrees"}
    # By language, as forms' are: only the portal's languages.
    wrong = {"name": "Uni", "description": {"fr": "Diplômes"}}
    refused = await client.post(f"/api/v1/tenants/{tenant}/issuers", json=wrong, headers=headers)
    assert refused.status_code == 422
    as_text = {"name": "Uni", "description": "Degrees"}
    refused = await client.post(f"/api/v1/tenants/{tenant}/issuers", json=as_text, headers=headers)
    assert refused.status_code == 422


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


async def test_an_identity_shows_its_did_document(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    issuer = (await client.post(f"{base}/issuers", json={"name": "Uni"}, headers=headers)).json()
    identity_url = f"{base}/identities/{issuer['identity']['id']}"

    response = await client.get(identity_url, headers=headers)
    assert response.status_code == 200, response.text
    pending = response.json()
    assert pending["name"] == "Uni"
    assert pending["used_by"] == [{"kind": "issuer", "id": issuer["id"], "name": "Uni"}]
    # No DID until an admin signs it: the document is written with its template.
    assert pending["did"] is None and pending["signature"] == "pending"
    assert pending["log_url"] is None and pending["document_url"] is None
    host = quote(urlsplit(get_settings().did_url).netloc, safe="")
    assert pending["document"]["id"].startswith(f"did:webvh:{{SCID}}:{host}:ids:idn_")
    published = await client.post(
        f"{base}/issuers/{issuer['id']}/publish", json={}, headers=headers
    )
    assert published.status_code == 409 and published.json()["detail"] == "identity_pending"
    waiting = (await client.get(f"{base}/signatures", headers=headers)).json()
    assert {(w["name"], w["signature"]) for w in waiting} >= {("Uni", "pending")}

    # Ada links a wallet; she signs the tenant's identity, then the issuer's.
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    own = (await client.get(base, headers=headers)).json()["identity"]["id"]
    await sign(client, headers, tenant, own, wallet)
    tenant_did = (await client.get(f"{base}/identities/{own}", headers=headers)).json()["did"]
    request = await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    assert request["sign"]["version"] == 1 and request["sign"]["did"] is None

    detail = (await client.get(identity_url, headers=headers)).json()
    did = detail["did"]
    assert did.startswith("did:webvh:Qm") and f":{host}:ids:idn_" in did
    assert detail["signature"] == "signed"
    # Its tenant controls it, and presents for it with its admins' keys; the
    # messaging key made when it was signed is what messages are encrypted to.
    key = wallet.did.removeprefix("did:key:")
    agreement = detail["document"]["keyAgreement"][0].removeprefix(f"{did}#")
    assert agreement.startswith("z6LS")
    assert detail["document"] == {
        "@context": ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/multikey/v1"],
        "id": did,
        "controller": tenant_did,
        "verificationMethod": [
            {
                "id": f"{did}#{agreement}",
                "type": "Multikey",
                "controller": did,
                "publicKeyMultibase": agreement,
            }
        ],
        "authentication": [f"{tenant_did}#{key}"],
        "keyAgreement": [f"{did}#{agreement}"],
    }

    # Public once published: the log, and the document under its did:web name.
    await publish(client, headers, tenant, "issuers", issuer["id"], wallet)
    slug = did.rsplit(":", 1)[1]
    assert detail["log_url"] == f"{get_settings().did_url}/ids/{slug}/did.jsonl"
    log = await client.get(f"/ids/{slug}/did.jsonl")
    assert log.status_code == 200 and log.headers["content-type"].startswith("text/jsonl")
    entries = [json.loads(line) for line in log.text.splitlines()]
    assert [e["versionId"].split("-")[0] for e in entries] == ["1"]
    assert entries[0]["parameters"]["updateKeys"] == [wallet.did.removeprefix("did:key:")]
    web = await client.get(f"/ids/{slug}/did.json")
    assert web.headers["content-type"] == "application/did+json"
    assert web.json()["id"] == f"did:web:{host}:ids:{slug}"
    assert web.json()["alsoKnownAs"] == [did]

    # A change to what it should publish waits for a signature.
    mediator = await new_mediator(client, headers, tenant, dns)
    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    await publish(client, headers, tenant, "mediators", mediator["id"], wallet)
    patch = {"mediator_id": mediator["id"]}
    await client.patch(f"{base}/issuers/{issuer['id']}", json=patch, headers=headers)
    outdated = (await client.get(identity_url, headers=headers)).json()
    assert outdated["signature"] == "outdated"
    # What it should say now, beside what was last signed.
    assert "service" in outdated["document"]
    assert "service" not in outdated["signed_document"]
    assert outdated["changes"] == ["service"]
    second = await sign(client, headers, tenant, issuer["identity"]["id"], wallet)
    assert second["sign"]["version"] == 2 and second["sign"]["did"] == did
    lines = (await client.get(f"/ids/{slug}/did.jsonl")).text.splitlines()
    assert len(lines) == 2
    assert (await client.get(identity_url, headers=headers)).json()["signature"] == "signed"
    waiting = (await client.get(f"{base}/signatures", headers=headers)).json()
    assert issuer["identity"]["id"] not in [w["id"] for w in waiting]
    nothing = await client.post(f"{identity_url}/sign", json={}, headers=headers)
    assert nothing.status_code == 409 and nothing.json()["detail"] == "up_to_date"


async def test_the_did_document_names_the_mediator(
    client: AsyncClient, outbox: Outbox, dns: Dns
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = await ready(client, headers, tenant)
    mediator = await new_mediator(client, headers, tenant, dns)
    await client.patch(base, json={"mediator_id": mediator["id"]}, headers=headers)
    identity_id = (await client.get(base, headers=headers)).json()["identity"]["id"]
    # Nothing routes through a mediator whose DID does not resolve yet; the
    # domain it listens on is the tenant's, named in its document.
    detail = (await client.get(f"{base}/identities/{identity_id}", headers=headers)).json()
    assert [s["type"] for s in detail["document"]["service"]] == ["LinkedDomains"]

    await sign(client, headers, tenant, mediator["identity"]["id"], wallet)
    await publish(client, headers, tenant, "mediators", mediator["id"], wallet)
    detail = (await client.get(f"{base}/identities/{identity_id}", headers=headers)).json()
    assert detail["used_by"][0]["kind"] == "tenant"
    assert [s for s in detail["document"]["service"] if s["type"] != "LinkedDomains"] == [
        {
            "id": f"{detail['document']['id']}#didcomm",
            "type": "DIDCommMessaging",
            "serviceEndpoint": {
                "uri": "did:web:mediator.example.org",
                "accept": ["didcomm/v2"],
            },
        }
    ]


async def test_only_an_admin_with_a_wallet_signs(client: AsyncClient, outbox: Outbox) -> None:
    ada, tenant = await _sign_in(client, outbox, "ada@example.org")
    own = (await client.get(f"/api/v1/tenants/{tenant}", headers=ada)).json()["identity"]["id"]
    url = f"/api/v1/tenants/{tenant}/identities/{own}/sign"
    # No admin has a wallet yet: nobody could sign.
    none = await client.post(url, json={}, headers=ada)
    assert none.status_code == 409 and none.json()["detail"] == "no_signers"

    await link_wallet(client, ada, FakeWallet())
    await client.post(
        f"/api/v1/tenants/{tenant}/invitations",
        json={"email": "bob@example.org", "role": "admin"},
        headers=ada,
    )
    bob, _ = await _sign_in(client, outbox, "bob@example.org")
    # Bob is an admin without a wallet: not one of the update keys.
    refused = await client.post(url, json={}, headers=bob)
    assert refused.status_code == 403 and refused.json()["detail"] == "not_a_signer"

    # A wallet that is an update key but not the asking admin's is refused.
    asked = (await client.post(url, json={}, headers=ada)).json()
    request = (await client.get(asked["request_uri"])).json()
    answered = await client.post(request["response_uri"], json=FakeWallet().answer(request))
    assert answered.status_code == 400 and answered.json()["detail"] == "not_an_update_key"


async def test_identities_stay_in_their_tenant(client: AsyncClient, outbox: Outbox) -> None:
    ada, ada_tenant = await _sign_in(client, outbox, "ada@example.org")
    bob, bob_tenant = await _sign_in(client, outbox, "bob@example.org")
    own = (await client.get(f"/api/v1/tenants/{ada_tenant}", headers=ada)).json()["identity"]["id"]

    for path in (f"{bob_tenant}/identities/{own}", f"{ada_tenant}/identities/{own}"):
        response = await client.get(f"/api/v1/tenants/{path}", headers=bob)
        assert response.status_code == 404
    assert (await client.get("/ids/idn_nothere/did.json")).status_code == 404


@pytest.mark.parametrize("kind", ["issuers", "verifiers"])
async def test_one_opens_and_changes(
    client: AsyncClient, outbox: Outbox, dns: Dns, kind: str
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    mediator = await new_mediator(client, headers, tenant, dns)
    created = (await client.post(f"{base}/{kind}", json={"name": "Uni"}, headers=headers)).json()
    url = f"{base}/{kind}/{created['id']}"

    detail = (await client.get(url, headers=headers)).json()
    assert detail["name"] == "Uni" and detail["mediator"] is None
    assert detail["did"] is None and detail["signature"] == "pending"

    patch = {"name": " Uni 2 ", "description": {"en": "Degrees"}, "mediator_id": mediator["id"]}
    changed = await client.patch(url, json=patch, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Uni 2"
    assert changed.json()["description"] == {"en": "Degrees"}
    assert changed.json()["mediator"] == {"id": mediator["id"], "name": "Relay", "own": True}
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
