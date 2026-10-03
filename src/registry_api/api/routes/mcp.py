"""The MCP endpoint (Streamable HTTP, stateless): the registry's tools for AI clients.

Every message is one JSON-RPC request answered as JSON (no SSE stream, no
session: each request stands alone). It needs the same bearer token as the
API, an API token or a sign-in, and acts as its account.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse

from registry_api import __version__, mcp
from registry_api.api.routes.auth import current_session
from registry_api.config import get_settings

router = APIRouter(tags=["mcp"])

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _result(message_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": message_id, "result": result})


def _error(
    message_id: Any, code: int, message: str, http: int = status.HTTP_200_OK
) -> JSONResponse:
    error = {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
    return JSONResponse(error, status_code=http)


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocolVersion")
    version = asked if asked in mcp.PROTOCOL_VERSIONS else mcp.PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": "almena-registry",
            "title": "Almena ID registry",
            "version": __version__,
        },
        "instructions": mcp.INSTRUCTIONS,
    }


@router.post(
    "/mcp",
    summary="MCP (Streamable HTTP): the registry's operations as tools for AI clients",
    dependencies=[Depends(current_session)],
)
async def message(
    request: Request,
    origin: Annotated[str | None, Header()] = None,
    mcp_protocol_version: Annotated[str | None, Header()] = None,
) -> Response:
    # Against DNS rebinding: a browser may only call from the portals' origins.
    if origin is not None and origin not in get_settings().cors_origins:
        return _error(None, INVALID_REQUEST, "origin_not_allowed", status.HTTP_403_FORBIDDEN)
    if mcp_protocol_version is not None and mcp_protocol_version not in mcp.PROTOCOL_VERSIONS:
        return _error(
            None, INVALID_REQUEST, "unsupported_protocol_version", status.HTTP_400_BAD_REQUEST
        )
    try:
        body = await request.json()
    except ValueError:
        return _error(None, PARSE_ERROR, "parse_error", status.HTTP_400_BAD_REQUEST)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "invalid_request", status.HTTP_400_BAD_REQUEST)

    # Notifications and responses (no method or no id) are only acknowledged.
    if "method" not in body or "id" not in body:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    message_id = body["id"]
    method = body["method"]
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _error(message_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return _result(message_id, _initialize(params))
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": mcp.tool_list(request.app)})
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        if not isinstance(params.get("name"), str) or not isinstance(arguments, dict):
            return _error(message_id, INVALID_PARAMS, "name and arguments are required")
        try:
            result = await mcp.call(
                request.app, params["name"], arguments, request.headers["authorization"]
            )
        except mcp.ToolArgumentsError as error:
            return _error(message_id, INVALID_PARAMS, str(error))
        return _result(message_id, result)
    return _error(message_id, METHOD_NOT_FOUND, f"method not found: {method}")


@router.api_route(
    "/mcp",
    methods=["GET", "DELETE"],
    summary="No SSE stream and no session to end: only POST",
    include_in_schema=False,
)
async def not_allowed() -> Response:
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})
