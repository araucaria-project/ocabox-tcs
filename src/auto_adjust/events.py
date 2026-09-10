"""Event types and a tiny pub-sub bus for cross-cutting notifications.

Adapters and applications use events to coordinate cache invalidation,
recalibration triggers, and similar concerns that don't fit cleanly into
predict/record. Adapters implement `notify_event(event_name, **payload)`
to receive events; applications publish events via an `EventBus` instance.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """Standard event names. Adapters may also accept custom names —
    these are the *recommended* set for cross-application interoperability.
    """

    # Calibration lifecycle
    CALIBRATION_STARTED = "calibration_started"
    CALIBRATION_COMPLETE = "calibration_complete"
    CALIBRATION_FAILED = "calibration_failed"

    # Drift / quality
    DRIFT_DETECTED = "drift_detected"
    UNCERTAINTY_HIGH = "uncertainty_high"
    OUTLIER_REJECTED = "outlier_rejected"

    # Domain context (typical externally-triggered)
    POSE_CHANGED_LARGE = "pose_changed_large"
    INSTRUMENT_RECONFIGURED = "instrument_reconfigured"
    ENVIRONMENT_FINGERPRINT_MISMATCH = "environment_fingerprint_mismatch"


class EventBus:
    """Tiny synchronous pub-sub.

    Not thread-safe. Application is expected to publish from a single
    thread / coroutine. Subscribers' exceptions are logged and otherwise
    swallowed so a misbehaving subscriber doesn't break the publisher.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = {}

    def subscribe(self, event: str, callback: Callable[..., None]) -> Callable[[], None]:
        """Subscribe `callback` to `event`. Returns an unsubscribe function."""
        self._subscribers.setdefault(event, []).append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers[event].remove(callback)
            except (KeyError, ValueError):
                pass

        return _unsubscribe

    def publish(self, event: str, **payload: Any) -> None:
        """Publish `event` to all subscribers. Subscribers' exceptions are
        logged via the standard `logging` module and otherwise swallowed.
        """
        import logging

        log = logging.getLogger(__name__)
        for callback in list(self._subscribers.get(event, ())):
            try:
                callback(event=event, **payload)
            except Exception as e:  # noqa: BLE001 — defensive isolation
                log.exception("Subscriber for %r raised: %r", event, e)
