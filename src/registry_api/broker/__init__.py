"""The message broker issuers' and verifiers' back offices read from.

Each issuer and each verifier may have a queue of its own, `subject.{slug}`,
read by a user named like it (its slug) that may read that queue and nothing
else, so a different system can serve each. The platform puts in it what
happens to that issuer or verifier (`registry_api.queues`). `Broker` (base.py)
is the interface; RabbitMQ (rabbitmq.py) the service; tests use memory.py.
"""

from functools import lru_cache

from registry_api.broker.base import Broker, BrokerError
from registry_api.config import get_settings

__all__ = ["Broker", "BrokerError", "close_broker", "get_broker"]


@lru_cache
def _broker() -> Broker:
    from registry_api.broker.rabbitmq import RabbitMQBroker

    settings = get_settings()
    return RabbitMQBroker(
        settings.rabbitmq_api_url,
        settings.rabbitmq_admin,
        settings.rabbitmq_password.get_secret_value(),
        settings.rabbitmq_vhost,
    )


def get_broker() -> Broker:
    """FastAPI dependency: one broker client per process; tests override it."""
    return _broker()


async def close_broker() -> None:
    if _broker.cache_info().currsize:
        await _broker().aclose()
        _broker.cache_clear()
