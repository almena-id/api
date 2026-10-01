import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from starlette.types import Receive, Scope, Send

from registry_api.logs import (
    REDACTED,
    JsonFormatter,
    RequestContext,
    RequestContextMiddleware,
    TextFormatter,
    set_tenant,
    set_user,
)
from tests.conftest import Outbox


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def access() -> Iterator[_Records]:
    handler = _Records()
    logger = logging.getLogger("registry_api.access")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "registry_api.test", logging.INFO, __file__, 1, "hello %s", ("you",), None
    )
    record.__dict__.update(extra)
    return record


def test_json_carries_the_context_and_redacts() -> None:
    user, tenant = uuid.uuid4(), uuid.uuid4()
    record = _record(
        context=RequestContext("req-1", user, tenant),
        email="someone@example.com",
        client_secret="s",
        body={"name": "Acme", "id_token": "eyJ…"},
        status=200,
        route=None,
    )
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "hello you"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "registry_api.test"
    assert entry["request_id"] == "req-1"
    assert entry["user_id"] == str(user)
    assert entry["tenant_id"] == str(tenant)
    assert entry["email"] == REDACTED
    assert entry["client_secret"] == REDACTED
    assert entry["body"] == {"name": "Acme", "id_token": REDACTED}
    assert entry["status"] == 200
    assert "route" not in entry


def test_json_leaves_out_what_there_is_not() -> None:
    entry = json.loads(JsonFormatter().format(_record(context=None)))
    assert "request_id" not in entry
    assert "user_id" not in entry
    assert "tenant_id" not in entry


def test_text_is_one_readable_line() -> None:
    record = _record(context=RequestContext("req-1"), status=200, token="t")
    line = TextFormatter().format(record)
    assert "INFO    registry_api.test [req-1] hello you" in line
    assert line.endswith(f"token={REDACTED}")


def test_setting_outside_a_request_does_nothing() -> None:
    set_user(uuid.uuid4())
    set_tenant(uuid.uuid4())


async def test_every_response_carries_a_request_id(client: AsyncClient, access: _Records) -> None:
    response = await client.get("/api/v1/auth/providers")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    [record] = access.records
    assert _context(record).request_id == request_id
    assert record.__dict__["method"] == "GET"
    assert record.__dict__["path"] == "/api/v1/auth/providers"
    assert record.__dict__["route"] == "/api/v1/auth/providers"
    assert record.__dict__["status"] == 200


async def test_a_sane_caller_id_is_kept(client: AsyncClient) -> None:
    headers = {"X-Request-ID": "portal-42.a_b"}
    response = await client.get("/api/v1/auth/providers", headers=headers)
    assert response.headers["x-request-id"] == "portal-42.a_b"


async def test_an_odd_caller_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/providers", headers={"X-Request-ID": "x" * 65})
    assert response.headers["x-request-id"] != "x" * 65


async def test_query_strings_are_not_logged(client: AsyncClient, access: _Records) -> None:
    await client.get("/api/v1/auth/providers?token=secret")
    [record] = access.records
    assert record.__dict__["path"] == "/api/v1/auth/providers"
    assert "secret" not in record.getMessage()


async def test_successful_health_checks_are_not_logged(
    client: AsyncClient, access: _Records
) -> None:
    await client.get("/health")
    assert access.records == []


def _context(record: logging.LogRecord) -> RequestContext:
    context: Any = getattr(record, "context", None)
    assert isinstance(context, RequestContext)
    return context


async def test_user_and_tenant_come_once_checked(
    client: AsyncClient, access: _Records, outbox: Outbox
) -> None:
    await client.post("/api/v1/auth/code", json={"email": "ada@example.org"})
    response = await client.post(
        "/api/v1/auth/verify", json={"email": "ada@example.org", "code": outbox.last_code()}
    )
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    [tenant] = (await client.get("/api/v1/tenants", headers=headers)).json()
    assert _context(access.records[-1]).user_id is not None
    assert _context(access.records[-1]).tenant_id is None
    access.records.clear()

    await client.get(f"/api/v1/tenants/{tenant['id']}/issuers", headers=headers)
    [record] = access.records
    assert _context(record).user_id is not None
    assert str(_context(record).tenant_id) == tenant["id"]
    assert record.__dict__["route"] == "/api/v1/tenants/{tenant_id}/issuers"

    access.records.clear()
    await client.get(f"/api/v1/tenants/{uuid.uuid4()}/issuers", headers=headers)
    [record] = access.records
    assert getattr(record, "status", None) == 404
    assert _context(record).user_id is not None
    assert _context(record).tenant_id is None


async def test_an_unhandled_error_is_one_error_record(access: _Records) -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await RequestContextMiddleware(broken)(
            {"type": "http", "method": "GET", "path": "/x", "headers": []},
            _nothing,
            _nothing,
        )
    [record] = access.records
    assert record.levelno == logging.ERROR
    assert record.__dict__["status"] == 500
    assert record.exc_info is not None


async def _nothing(*_: Any) -> Any:
    return None
