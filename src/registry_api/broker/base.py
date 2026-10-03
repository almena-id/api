"""What the platform asks of the broker: one queue per issuer or verifier, a
user that may only read it, and messages put in it."""

from abc import ABC, abstractmethod
from typing import Any


class BrokerError(Exception):
    """The broker could not be reached, or refused what it was asked."""


class Broker(ABC):
    @abstractmethod
    async def declare(self, queue: str) -> None:
        """Makes `queue` (durable); one that exists is left as it is."""

    @abstractmethod
    async def grant(self, user: str, password: str, queue: str) -> None:
        """Makes `user` with `password` — or gives one that exists that
        password — allowed to read `queue` and nothing else."""

    @abstractmethod
    async def remove(self, user: str, queue: str) -> None:
        """Deletes `queue`, with what is in it, and `user`; either may be gone."""

    @abstractmethod
    async def publish(self, queue: str, kind: str, message: dict[str, Any]) -> None:
        """Puts `message` (JSON) in `queue`, persistent, typed `kind`."""

    async def aclose(self) -> None:
        return None
