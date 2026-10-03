import json
from typing import Any

from httpx import AsyncClient

from registry_api import mcp
from registry_api.config import get_settings
from tests.conftest import Outbox
from tests.test_account import _sign_in

Headers = dict[str, str]


async def _rpc(
    client: AsyncClient, headers: Headers, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    response = await client.post("/mcp", json=message, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["id"] == 1
    return body


async def _call(
    client: AsyncClient, headers: Headers, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    body = await _rpc(client, headers, "tools/call", {"name": name, "arguments": arguments})
    result: dict[str, Any] = body["result"]
    return result


async def test_it_needs_a_bearer_token(client: AsyncClient) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = await client.post("/mcp", json=message)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_initialize_agrees_a_protocol_version(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    known = await _rpc(client, ada, "initialize", {"protocolVersion": "2025-06-18"})
    assert known["result"]["protocolVersion"] == "2025-06-18"
    assert known["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert known["result"]["serverInfo"]["name"] == "almena-registry"
    unknown = await _rpc(client, ada, "initialize", {"protocolVersion": "1999-01-01"})
    assert unknown["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    assert (await _rpc(client, ada, "ping"))["result"] == {}


async def test_every_tool_is_a_route_with_its_schema(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tools = (await _rpc(client, ada, "tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == [t.name for t in mcp.TOOLS]
    by_name = {t["name"]: t for t in tools}

    create = by_name["create_issuer"]
    assert create["inputSchema"]["required"] == ["tenant_id", "body"]
    body_ref = create["inputSchema"]["properties"]["body"]["$ref"]
    assert body_ref.startswith("#/$defs/")
    assert body_ref.removeprefix("#/$defs/") in create["inputSchema"]["$defs"]
    assert create["annotations"]["readOnlyHint"] is False
    assert by_name["list_issuers"]["annotations"]["readOnlyHint"] is True
    assert by_name["update_tenant"]["annotations"]["destructiveHint"] is True
    assert "limit" in by_name["list_issuers"]["inputSchema"]["properties"]

    # Nothing that deletes, unpublishes, decides, or touches the account's ways in.
    assert not any(t.method not in ("GET", "POST", "PUT", "PATCH") for t in mcp.TOOLS)
    for left_out in ("/unpublish", "/decision", "/auth/me/tokens", "/auth/wallet"):
        assert not any(left_out in t.path for t in mcp.TOOLS)


async def test_a_tool_answers_as_its_route(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    tenants = await _call(client, ada, "list_tenants", {})
    assert tenants["isError"] is False
    tenant_id = json.loads(tenants["content"][0]["text"])[0]["id"]

    made = await _call(
        client, ada, "create_issuer", {"tenant_id": tenant_id, "body": {"name": "Town hall"}}
    )
    assert made["isError"] is False, made
    issuer = made["structuredContent"]
    assert issuer["name"] == "Town hall"

    listed = await _call(client, ada, "list_issuers", {"tenant_id": tenant_id, "limit": 5})
    assert [i["id"] for i in listed["structuredContent"]["items"]] == [issuer["id"]]
    # The REST API sees the same.
    rest = await client.get(f"/api/v1/tenants/{tenant_id}/issuers", headers=ada)
    assert rest.json()["items"][0]["id"] == issuer["id"]


async def test_the_api_checks_what_a_tool_is_sent(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    bob = await _sign_in(client, outbox, "bob@example.org")
    tenants = await _call(client, ada, "list_tenants", {})
    tenant_id = json.loads(tenants["content"][0]["text"])[0]["id"]

    blank = await _call(
        client, ada, "create_issuer", {"tenant_id": tenant_id, "body": {"name": " "}}
    )
    assert blank["isError"] is True
    assert blank["content"][0]["text"].startswith("HTTP 422")
    assert "structuredContent" not in blank

    # Another account's tenant is not found, as through the API.
    foreign = await _call(client, bob, "get_tenant", {"tenant_id": tenant_id})
    assert foreign["isError"] is True
    assert "tenant_not_found" in foreign["content"][0]["text"]


async def test_bad_calls_are_json_rpc_errors(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    missing = await _rpc(client, ada, "tools/call", {"name": "get_tenant", "arguments": {}})
    assert missing["error"] == {"code": -32602, "message": "tenant_id is required"}
    unknown = await _rpc(client, ada, "tools/call", {"name": "drop_everything"})
    assert unknown["error"]["code"] == -32602
    no_body = await _rpc(
        client, ada, "tools/call", {"name": "create_issuer", "arguments": {"tenant_id": "x"}}
    )
    assert no_body["error"] == {"code": -32602, "message": "body is required"}
    assert (await _rpc(client, ada, "resources/list"))["error"]["code"] == -32601


async def test_the_transport(client: AsyncClient, outbox: Outbox) -> None:
    ada = await _sign_in(client, outbox, "ada@example.org")
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert (await client.post("/mcp", json=note, headers=ada)).status_code == 202
    assert (await client.get("/mcp", headers=ada)).status_code == 405
    assert (await client.delete("/mcp", headers=ada)).status_code == 405

    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    foreign = await client.post(
        "/mcp", json=ping, headers={**ada, "Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    origin = get_settings().cors_origins[0]
    allowed = await client.post("/mcp", json=ping, headers={**ada, "Origin": origin})
    assert allowed.status_code == 200
    old = await client.post("/mcp", json=ping, headers={**ada, "MCP-Protocol-Version": "2020"})
    assert old.status_code == 400
    garbled = await client.post(
        "/mcp", content=b"{", headers={**ada, "Content-Type": "application/json"}
    )
    assert garbled.status_code == 400
    assert garbled.json()["error"]["code"] == -32700
