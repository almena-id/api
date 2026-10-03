"""RabbitMQ, through its management HTTP API, as the platform's own account.

Queues are durable classic queues in one virtual host; each one's user may
read it and nothing else (`configure` and `write` match no name; the
default exchange is checked as `amq.default`, so it cannot publish either). Messages
are published to the default exchange, routed by the queue's name,
persistent, as JSON.
"""

import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from registry_api.broker.base import Broker, BrokerError


class RabbitMQBroker(Broker):
    def __init__(
        self,
        api_url: str,
        admin: str,
        password: str,
        vhost: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/") + "/api",
            auth=(admin, password),
            timeout=5.0,
            transport=transport,
        )
        self._vhost = quote(vhost, safe="")

    async def _request(
        self, method: str, url: str, body: dict[str, Any] | None = None, *, missing_ok: bool = False
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, url, json=body)
        except httpx.HTTPError as error:
            raise BrokerError(f"RabbitMQ unreachable: {type(error).__name__}") from error
        if response.status_code == 404 and missing_ok:
            return response
        if response.status_code not in (200, 201, 204):
            raise BrokerError(f"RabbitMQ refused {method} {url}: {response.status_code}")
        return response

    @staticmethod
    def _name(name: str) -> str:
        return quote(name, safe="")

    async def declare(self, queue: str) -> None:
        await self._request("PUT", f"/queues/{self._vhost}/{self._name(queue)}", {"durable": True})

    async def grant(self, user: str, password: str, queue: str) -> None:
        await self._request("PUT", f"/users/{self._name(user)}", {"password": password, "tags": ""})
        only = "^" + queue.replace(".", "\\.") + "$"
        await self._request(
            "PUT",
            f"/permissions/{self._vhost}/{self._name(user)}",
            {"configure": "^$", "write": "^$", "read": only},
        )

    async def remove(self, user: str, queue: str) -> None:
        await self._request("DELETE", f"/users/{self._name(user)}", missing_ok=True)
        await self._request("DELETE", f"/queues/{self._vhost}/{self._name(queue)}", missing_ok=True)

    async def publish(self, queue: str, kind: str, message: dict[str, Any]) -> None:
        response = await self._request(
            "POST",
            f"/exchanges/{self._vhost}/amq.default/publish",
            {
                "properties": {
                    "delivery_mode": 2,
                    "content_type": "application/json",
                    "message_id": str(uuid.uuid4()),
                    "type": kind,
                },
                "routing_key": queue,
                "payload": json.dumps(message, separators=(",", ":"), ensure_ascii=False),
                "payload_encoding": "string",
            },
        )
        if not response.json().get("routed"):
            raise BrokerError(f"RabbitMQ has no queue {queue}")

    async def aclose(self) -> None:
        await self._http.aclose()
