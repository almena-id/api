"""The registry's operations as MCP tools (Model Context Protocol).

Each tool is one route of the REST API: its input schema is built from that
route's OpenAPI operation (path and query parameters, and its JSON body as
`body`), and calling it sends the route a request in process, with the
caller's bearer token. So a tool is checked and answered exactly as the route
is: the same authentication, membership, validation and errors.

Only reads and operations a person can still stop are tools: deleting,
unpublishing, deciding applications, queue passwords, and the account's own
ways in, tokens and wallet are left to the portal and the CLI. Operations that end in a
signature (publishing, signing an identity, issuing) only make the wallet
request; the signer approves it in their wallet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI

from registry_api import logs

Method = Literal["GET", "POST", "PUT", "PATCH"]

# Newest first: the version the server answers with when the client's is unknown.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

INSTRUCTIONS = (
    "Tools of the Almena ID registry, acting as the account whose API token is used. "
    "Start with list_tenants: almost every other tool takes a tenant_id. Issuers, "
    "verifiers and mediators are drafts until published. Tools that end in a signature "
    "(publish, sign_identity, request_issuance_signature, sign_status_list, "
    "set_credential_status) return a wallet request: show "
    "its deep_link to the person who signs as the tenant, who approves it in their "
    "Almena wallet. Errors come back as the API's own, with a code such as "
    "mediator_not_found."
)

T = "/api/v1/tenants/{tenant_id}"


@dataclass(frozen=True)
class Tool:
    name: str
    method: Method
    path: str

    @property
    def read_only(self) -> bool:
        return self.method == "GET"


TOOLS: tuple[Tool, ...] = (
    # The account and its tenants.
    Tool("get_account", "GET", "/api/v1/auth/me"),
    Tool("list_tenants", "GET", "/api/v1/tenants"),
    Tool("get_tenant", "GET", T),
    Tool("update_tenant", "PATCH", T),
    Tool("get_tenant_health", "GET", f"{T}/health"),
    Tool("list_members", "GET", f"{T}/members"),
    Tool("invite_member", "POST", f"{T}/invitations"),
    # Domains.
    Tool("list_domains", "GET", f"{T}/domains"),
    Tool("add_domain", "POST", f"{T}/domains"),
    Tool("check_domain", "POST", f"{T}/domains/{{domain_id}}/check"),
    # The directory.
    Tool("list_issuers", "GET", f"{T}/issuers"),
    Tool("get_issuer", "GET", f"{T}/issuers/{{issuer_id}}"),
    Tool("create_issuer", "POST", f"{T}/issuers"),
    Tool("update_issuer", "PATCH", f"{T}/issuers/{{issuer_id}}"),
    Tool("list_verifiers", "GET", f"{T}/verifiers"),
    Tool("get_verifier", "GET", f"{T}/verifiers/{{verifier_id}}"),
    Tool("create_verifier", "POST", f"{T}/verifiers"),
    Tool("update_verifier", "PATCH", f"{T}/verifiers/{{verifier_id}}"),
    Tool("list_mediators", "GET", f"{T}/mediators"),
    Tool("get_mediator", "GET", f"{T}/mediators/{{mediator_id}}"),
    Tool("create_mediator", "POST", f"{T}/mediators"),
    Tool("update_mediator", "PATCH", f"{T}/mediators/{{mediator_id}}"),
    Tool("list_mediator_choices", "GET", f"{T}/mediator-choices"),
    Tool("list_identities", "GET", f"{T}/identities"),
    Tool("get_identity", "GET", f"{T}/identities/{{identity_id}}"),
    Tool("create_identity", "POST", f"{T}/identities"),
    Tool("list_pending", "GET", f"{T}/pending"),
    Tool("sign_identity", "POST", f"{T}/identities/{{identity_id}}/sign"),
    Tool("get_signing", "GET", f"{T}/{{kind}}/{{item_id}}/signing"),
    Tool("set_signing", "PUT", f"{T}/{{kind}}/{{item_id}}/signing"),
    Tool("get_queue", "GET", f"{T}/{{kind}}/{{item_id}}/queue"),
    Tool("open_verification", "POST", f"{T}/verifiers/{{verifier_id}}/verifications"),
    Tool(
        "get_verification",
        "GET",
        f"{T}/verifiers/{{verifier_id}}/verifications/{{verification_id}}",
    ),
    Tool("publish", "POST", f"{T}/{{kind}}/{{item_id}}/publish"),
    Tool("list_published", "GET", "/api/v1/catalog/{kind}"),
    Tool("list_offers", "GET", "/api/v1/catalog/offers"),
    # Catalogues, fields and forms.
    Tool("get_field_catalogue", "GET", "/api/v1/catalog/fields"),
    Tool("get_credential_catalogue", "GET", "/api/v1/catalog/credentials"),
    Tool("list_custom_fields", "GET", f"{T}/fields"),
    Tool("create_custom_field", "POST", f"{T}/fields"),
    Tool("update_custom_field", "PATCH", f"{T}/fields/{{field_id}}"),
    Tool("get_subscription", "GET", f"{T}/subscription"),
    Tool("list_accounts", "GET", f"{T}/accounts"),
    Tool("get_account", "GET", f"{T}/accounts/{{account_id}}"),
    Tool("set_account_subscription", "PUT", f"{T}/accounts/{{account_id}}/subscription"),
    Tool("list_value_domains", "GET", f"{T}/value-domains"),
    Tool("create_value_domain", "POST", f"{T}/value-domains"),
    Tool("update_value_domain", "PATCH", f"{T}/value-domains/{{domain_id}}"),
    Tool("list_categories", "GET", f"{T}/categories"),
    Tool("create_category", "POST", f"{T}/categories"),
    Tool("update_category", "PATCH", f"{T}/categories/{{category_id}}"),
    Tool("list_credential_types", "GET", f"{T}/credential-types"),
    Tool("create_credential_type", "POST", f"{T}/credential-types"),
    Tool("update_credential_type", "PATCH", f"{T}/credential-types/{{type_id}}"),
    Tool("list_forms", "GET", f"{T}/forms"),
    Tool("get_form", "GET", f"{T}/forms/{{form_id}}"),
    Tool("create_form", "POST", f"{T}/forms"),
    Tool("get_form_schema", "GET", f"{T}/forms/{{form_id}}/schema"),
    Tool("get_form_dcql", "GET", f"{T}/forms/{{form_id}}/dcql"),
    Tool("verify_presentations", "POST", f"{T}/forms/{{form_id}}/verify"),
    # Issuing.
    Tool("get_issuer_credential_types", "GET", f"{T}/issuers/{{issuer_id}}/credential-types"),
    Tool("set_issuer_credential_types", "PUT", f"{T}/issuers/{{issuer_id}}/credential-types"),
    Tool("list_applications", "GET", f"{T}/applications"),
    Tool("get_application", "GET", f"{T}/applications/{{application_id}}"),
    Tool("get_issuance_proposal", "GET", f"{T}/applications/{{application_id}}/issuance"),
    Tool("save_issuance_draft", "PUT", f"{T}/applications/{{application_id}}/issuance"),
    Tool(
        "request_issuance_signature", "POST", f"{T}/applications/{{application_id}}/issuance/sign"
    ),
    Tool("list_status_lists", "GET", f"{T}/issuers/{{issuer_id}}/status-lists"),
    Tool("sign_status_list", "POST", f"{T}/issuers/{{issuer_id}}/status-lists/sign"),
    Tool(
        "set_credential_status",
        "POST",
        f"{T}/applications/{{application_id}}/credential-status",
    ),
)

BY_NAME = {tool.name: tool for tool in TOOLS}


class ToolArgumentsError(ValueError):
    """The arguments do not fit the tool (a JSON-RPC invalid params error)."""


def _operation(openapi: dict[str, Any], tool: Tool) -> dict[str, Any]:
    try:
        operation: dict[str, Any] = openapi["paths"][tool.path][tool.method.lower()]
    except KeyError:
        raise LookupError(f"tool {tool.name}: no route {tool.method} {tool.path}") from None
    return operation


def _with_defs(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    """The schema with its `#/components/schemas/…` references made `#/$defs/…`,
    and those it reaches (directly or not) put under `$defs`."""
    defs: dict[str, Any] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "$ref" and value.startswith("#/components/schemas/"):
                    name = value.removeprefix("#/components/schemas/")
                    if name not in defs:
                        defs[name] = {}  # Taken before walking it: references can loop.
                        defs[name] = walk(components[name])
                    out[key] = f"#/$defs/{name}"
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    walked: dict[str, Any] = walk(schema)
    if defs:
        walked["$defs"] = defs
    return walked


def describe(openapi: dict[str, Any], tool: Tool) -> dict[str, Any]:
    """The tool as `tools/list` gives it."""
    operation = _operation(openapi, tool)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in operation.get("parameters", []):
        if parameter["in"] not in ("path", "query"):
            continue
        schema = dict(parameter.get("schema", {}))
        if "description" in parameter:
            schema["description"] = parameter["description"]
        properties[parameter["name"]] = schema
        if parameter.get("required"):
            required.append(parameter["name"])
    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if body is not None:
        properties["body"] = body["schema"]
        if operation["requestBody"].get("required"):
            required.append("body")
    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    components = openapi.get("components", {}).get("schemas", {})

    summary: str = operation.get("summary", tool.name)
    details: str = operation.get("description", "")
    return {
        "name": tool.name,
        "title": summary,
        "description": f"{summary}.\n\n{details}".strip() if details else summary,
        "inputSchema": _with_defs(input_schema, components),
        "annotations": {
            "readOnlyHint": tool.read_only,
            "destructiveHint": tool.method in ("PUT", "PATCH"),
            "idempotentHint": tool.method in ("GET", "PUT", "PATCH"),
            "openWorldHint": False,
        },
    }


def tool_list(app: FastAPI) -> list[dict[str, Any]]:
    openapi = app.openapi()
    return [describe(openapi, tool) for tool in TOOLS]


def _request(
    openapi: dict[str, Any], tool: Tool, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], Any]:
    """The route's path, query and JSON body for these arguments."""
    operation = _operation(openapi, tool)
    path = tool.path
    query: dict[str, Any] = {}
    for parameter in operation.get("parameters", []):
        name = parameter["name"]
        value = arguments.get(name)
        if parameter["in"] == "path":
            if value is None or value == "":
                raise ToolArgumentsError(f"{name} is required")
            path = path.replace(f"{{{name}}}", quote(str(value), safe=""))
        elif parameter["in"] == "query" and value is not None:
            query[name] = value
    has_body = "application/json" in operation.get("requestBody", {}).get("content", {})
    if has_body and "body" in arguments:
        if not isinstance(arguments["body"], dict):
            raise ToolArgumentsError("body must be an object")
        return path, query, arguments["body"]
    if has_body and operation["requestBody"].get("required"):
        raise ToolArgumentsError("body is required")
    return path, query, None


async def call(
    app: FastAPI, name: str, arguments: dict[str, Any], authorization: str
) -> dict[str, Any]:
    """Call a tool: its route answers, and its answer is the tool's result."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolArgumentsError(f"unknown tool: {name}")
    path, query, body = _request(app.openapi(), tool, arguments)
    headers = {"Authorization": authorization}
    if (request_id := logs.request_id()) is not None:
        headers[logs.REQUEST_ID_HEADER] = request_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://registry") as client:
        response = await client.request(tool.method, path, params=query, json=body, headers=headers)

    if response.status_code == 204 or not response.content:
        text = "Done." if response.is_success else f"HTTP {response.status_code}"
        return {"content": [{"type": "text", "text": text}], "isError": not response.is_success}
    try:
        answer: Any = response.json()
    except ValueError:
        answer = response.text
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    if not response.is_success:
        text = f"HTTP {response.status_code}: {text}"
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": not response.is_success,
    }
    if response.is_success and isinstance(answer, dict):
        result["structuredContent"] = answer
    return result
