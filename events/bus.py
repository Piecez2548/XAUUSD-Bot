"""In-process typed event bus with isolated optional subscribers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from domain.events import DomainEvent

EventHandler = Callable[[DomainEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Subscription:
    name: str
    handler: EventHandler
    critical: bool = False


class EventBus:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._subscriptions: list[Subscription] = []
        self._logger = logger or logging.getLogger(__name__)

    def subscribe(self, name: str, handler: EventHandler, *, critical: bool = False) -> None:
        if any(item.name == name for item in self._subscriptions):
            raise ValueError(f"event subscriber {name!r} is already registered")
        self._subscriptions.append(Subscription(name, handler, critical))

    async def publish(self, event: DomainEvent) -> None:
        for subscription in self._subscriptions:
            try:
                await subscription.handler(event)
            except Exception:
                self._logger.exception(
                    "Event subscriber %s failed for %s",
                    subscription.name,
                    event.event_type.value,
                )
                if subscription.critical:
                    raise
