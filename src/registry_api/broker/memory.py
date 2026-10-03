"""A broker in memory, for tests: what each queue holds, and who may read it."""

from typing import Any

from registry_api.broker.base import Broker, BrokerError


class MemoryBroker(Broker):
    def __init__(self) -> None:
        self.queues: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.users: dict[str, tuple[str, str]] = {}
        # Set to make every call fail, as an unreachable broker would.
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise BrokerError("down")

    async def declare(self, queue: str) -> None:
        self._check()
        self.queues.setdefault(queue, [])

    async def grant(self, user: str, password: str, queue: str) -> None:
        self._check()
        self.users[user] = (password, queue)

    async def remove(self, user: str, queue: str) -> None:
        self._check()
        self.users.pop(user, None)
        self.queues.pop(queue, None)

    async def publish(self, queue: str, kind: str, message: dict[str, Any]) -> None:
        self._check()
        if queue not in self.queues:
            raise BrokerError(f"no queue {queue}")
        self.queues[queue].append((kind, message))
