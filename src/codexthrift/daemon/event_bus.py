from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, capacity: int = 256) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._sequence = 0

    def publish(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            self._sequence += 1
            event = {"sequence": self._sequence, "type": event_type, "data": data}
            self._events.append(event)
            self._condition.notify_all()
            return event

    def wait(self, *, after: int = 0, timeout: float = 0.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(timeout, 30.0))
        with self._condition:
            while not any(event["sequence"] > after for event in self._events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            events = [event for event in self._events if event["sequence"] > after]
            return {"events": events, "latestSequence": self._sequence}
