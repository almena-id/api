import json
from typing import Any

import httpx
import pytest

from registry_api.broker import BrokerError
from registry_api.broker.rabbitmq import RabbitMQBroker


def _broker(answer: int = 201, routed: bool = True) -> tuple[RabbitMQBroker, list[Any]]:
    seen: list[Any] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.raw_path.decode(), body))
        if request.url.path.endswith("/publish"):
            return httpx.Response(200, json={"routed": routed})
        if request.method == "DELETE":
            return httpx.Response(404)
        return httpx.Response(answer)

    broker = RabbitMQBroker(
        "http://rabbit:15672", "almena", "secret", "almena", transport=httpx.MockTransport(handle)
    )
    return broker, seen


async def test_queues_users_and_messages_go_through_the_management_api() -> None:
    broker, seen = _broker()
    await broker.declare("subject.iss_abc")
    await broker.grant("iss_abc", "pw", "subject.iss_abc")
    await broker.publish("subject.iss_abc", "application.decided", {"a": "ñ"})
    # Gone already is gone.
    await broker.remove("iss_abc", "subject.iss_abc")
    assert seen[0] == ("PUT", "/api/queues/almena/subject.iss_abc", {"durable": True})
    assert seen[1] == ("PUT", "/api/users/iss_abc", {"password": "pw", "tags": ""})
    # It reads its queue and nothing else; it configures and writes nothing.
    assert seen[2] == (
        "PUT",
        "/api/permissions/almena/iss_abc",
        {"configure": "^$", "write": "^$", "read": "^subject\\.iss_abc$"},
    )
    method, path, body = seen[3]
    assert (method, path) == ("POST", "/api/exchanges/almena/amq.default/publish")
    assert body["routing_key"] == "subject.iss_abc"
    assert body["properties"]["delivery_mode"] == 2
    assert body["properties"]["type"] == "application.decided"
    assert json.loads(body["payload"]) == {"a": "ñ"}
    assert [(m, p) for m, p, _ in seen[4:]] == [
        ("DELETE", "/api/users/iss_abc"),
        ("DELETE", "/api/queues/almena/subject.iss_abc"),
    ]


async def test_a_broker_that_refuses_or_routes_nowhere_is_an_error() -> None:
    refusing, _ = _broker(answer=401)
    with pytest.raises(BrokerError):
        await refusing.declare("subject.iss_abc")
    nowhere, _ = _broker(routed=False)
    with pytest.raises(BrokerError):
        await nowhere.publish("subject.iss_abc", "x", {})
