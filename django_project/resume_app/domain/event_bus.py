"""
Lightweight In-Process Domain Event Bus for decoupling cross-aggregate side effects.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Type

from .events import DomainEvent

logger = logging.getLogger(__name__)


class DomainEventBus:
    """
    In-process publisher/subscriber event dispatcher.
    Supports registering synchronous handlers that process domain events.
    """

    def __init__(self):
        self._subscribers: dict[Type[DomainEvent], list[Callable[[DomainEvent], None]]] = defaultdict(list)

    def subscribe(self, event_type: Type[DomainEvent], handler: Callable[[Any], None]) -> None:
        """Register a handler for a specific domain event type."""
        if handler not in self._subscribers[event_type]:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: Type[DomainEvent], handler: Callable[[Any], None]) -> None:
        """Remove a registered handler."""
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    def publish(self, event: DomainEvent) -> None:
        """
        Publish a domain event to all registered listeners.
        Exceptions in handlers are caught and logged so one listener failure
        does not crash the primary domain transaction.
        """
        handlers = self._subscribers.get(type(event), [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.exception(
                    "[DomainEventBus] Handler '%s' failed for event '%s': %s",
                    getattr(handler, "__name__", str(handler)),
                    type(event).__name__,
                    exc,
                )

    def clear(self) -> None:
        """Clear all registered subscribers (useful in test teardown)."""
        self._subscribers.clear()


# Global singleton instance
event_bus = DomainEventBus()
