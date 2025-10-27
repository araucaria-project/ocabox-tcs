"""NATS event collection and verification helpers.

Provides utilities for collecting and querying NATS events during tests.
Supports filtering by event type, service ID, and time ranges.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from nats.aio.client import Client as NATS
from nats.js.api import DeliverPolicy

logger = logging.getLogger(__name__)


@dataclass
class CollectedEvent:
    """Represents a collected NATS event.

    Attributes:
        subject: NATS subject the event was published to
        data: Event data dictionary
        meta: Event metadata dictionary
        timestamp: Event timestamp (from data - event occurrence time)
        event_type: Event type (from data)
        service_id: Service identifier (from data)
    """
    subject: str
    data: dict[str, Any]
    meta: dict[str, Any]
    timestamp: list[int] = field(default_factory=list)
    event_type: str = ""
    service_id: str = ""

    def __post_init__(self):
        """Extract common fields from data and meta."""
        self.timestamp = self.data.get("timestamp", [])
        self.event_type = self.data.get("event", "")
        self.service_id = self.data.get("service_id", "")

    def matches(self,
                event_type: str | None = None,
                service_id: str | None = None,
                status: str | None = None) -> bool:
        """Check if event matches filter criteria.

        Args:
            event_type: Event type to match (e.g., "start", "crash")
            service_id: Service ID to match
            status: Status to match (e.g., "ok", "failed")

        Returns:
            True if event matches all specified criteria
        """
        if event_type and self.event_type != event_type:
            return False
        if service_id and self.service_id != service_id:
            return False
        if status and self.data.get("status") != status:
            return False
        return True


class NATSEventCollector:
    """Collects NATS events for test verification.

    Subscribes to NATS subjects and collects events for later querying.
    Supports filtering by event type, service ID, and other criteria.

    For tests, uses the 'test' stream which captures all test.> subjects.

    Args:
        nats_client: Connected NATS client
        stream_name: JetStream stream to read from (default: "test")
        subjects: List of subjects to subscribe to (supports wildcards)
        buffer_size: Maximum number of events to buffer (default: 1000)
    """

    def __init__(
        self,
        nats_client: NATS,
        stream_name: str = "test",
        subjects: list[str] | None = None,
        buffer_size: int = 1000
    ):
        self.nats_client = nats_client
        self.stream_name = stream_name
        self.subjects = subjects or ["test.>"]  # All test subjects by default
        self.buffer_size = buffer_size
        self.events: list[CollectedEvent] = []
        self._subscription = None
        self._collect_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start collecting events."""
        if self._running:
            logger.warning("Collector already running")
            return

        self._running = True
        js = self.nats_client.jetstream()

        # Create pull consumer for reading events
        try:
            # Subscribe to all requested subjects
            for subject in self.subjects:
                try:
                    # Create ephemeral pull consumer
                    psub = await js.pull_subscribe(
                        subject,
                        stream=self.stream_name
                    )
                    self._subscription = psub
                    logger.debug(f"Subscribed to {subject} on stream {self.stream_name}")
                except Exception as e:
                    logger.warning(f"Failed to subscribe to {subject}: {e}")

            # Start collection task
            self._collect_task = asyncio.create_task(self._collect_loop())

        except Exception as e:
            logger.error(f"Failed to start event collector: {e}")
            self._running = False
            raise

    async def stop(self):
        """Stop collecting events."""
        if not self._running:
            return

        self._running = False

        # Cancel collection task
        if self._collect_task:
            self._collect_task.cancel()
            try:
                await self._collect_task
            except asyncio.CancelledError:
                pass

        # Unsubscribe
        if self._subscription:
            try:
                await self._subscription.unsubscribe()
            except Exception as e:
                logger.warning(f"Error unsubscribing: {e}")

        logger.debug(f"Event collector stopped, collected {len(self.events)} events")

    async def _collect_loop(self):
        """Background task to collect events."""
        while self._running:
            try:
                if self._subscription:
                    # Fetch batch of messages
                    msgs = await self._subscription.fetch(batch=10, timeout=0.5)
                    for msg in msgs:
                        await self._process_message(msg)
                        await msg.ack()
                else:
                    await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                # No messages available, continue
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error collecting events: {e}")
                await asyncio.sleep(0.1)

    async def _process_message(self, msg):
        """Process collected message."""
        try:
            import json
            payload = json.loads(msg.data.decode())

            # Extract data and meta sections
            data = payload.get("data", {})
            meta = payload.get("meta", {})

            # Create event
            event = CollectedEvent(
                subject=msg.subject,
                data=data,
                meta=meta
            )

            # Add to buffer (FIFO, drop oldest if full)
            if len(self.events) >= self.buffer_size:
                self.events.pop(0)
            self.events.append(event)

            logger.debug(f"Collected event: {event.event_type} for {event.service_id}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def get_events(self,
                   event_type: str | None = None,
                   service_id: str | None = None,
                   status: str | None = None) -> list[CollectedEvent]:
        """Get collected events matching criteria.

        Args:
            event_type: Filter by event type
            service_id: Filter by service ID
            status: Filter by status

        Returns:
            List of matching events
        """
        return [
            event for event in self.events
            if event.matches(event_type, service_id, status)
        ]

    def get_latest_event(self,
                        event_type: str | None = None,
                        service_id: str | None = None,
                        status: str | None = None) -> CollectedEvent | None:
        """Get most recent event matching criteria.

        Args:
            event_type: Filter by event type
            service_id: Filter by service ID
            status: Filter by status

        Returns:
            Most recent matching event or None
        """
        matching = self.get_events(event_type, service_id, status)
        return matching[-1] if matching else None

    def count_events(self,
                    event_type: str | None = None,
                    service_id: str | None = None,
                    status: str | None = None) -> int:
        """Count events matching criteria.

        Args:
            event_type: Filter by event type
            service_id: Filter by service ID
            status: Filter by status

        Returns:
            Number of matching events
        """
        return len(self.get_events(event_type, service_id, status))

    def has_event(self,
                  event_type: str | None = None,
                  service_id: str | None = None,
                  status: str | None = None) -> bool:
        """Check if any event matches criteria.

        Args:
            event_type: Filter by event type
            service_id: Filter by service ID
            status: Filter by status

        Returns:
            True if at least one matching event exists
        """
        return self.count_events(event_type, service_id, status) > 0

    def get_event_sequence(self, service_id: str) -> list[str]:
        """Get sequence of event types for a service.

        Args:
            service_id: Service identifier

        Returns:
            List of event types in chronological order
        """
        return [
            event.event_type
            for event in self.get_events(service_id=service_id)
        ]

    def clear(self):
        """Clear all collected events."""
        self.events.clear()

    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.stop()
        return False


async def collect_registry_events(
    nats_client: NATS,
    timeout: float = 5.0,
    service_id: str | None = None,
    subject_prefix: str = "test.svc"
) -> list[CollectedEvent]:
    """Helper to collect registry events for a duration.

    Args:
        nats_client: Connected NATS client
        timeout: How long to collect events (seconds)
        service_id: Optional service ID to filter
        subject_prefix: NATS subject prefix (default: "test.svc")

    Returns:
        List of collected registry events
    """
    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=[f"{subject_prefix}.registry.>"]
    )

    async with collector:
        await asyncio.sleep(timeout)

    if service_id:
        return collector.get_events(service_id=service_id)
    return collector.events


async def collect_status_events(
    nats_client: NATS,
    timeout: float = 5.0,
    service_id: str | None = None,
    subject_prefix: str = "test.svc"
) -> list[CollectedEvent]:
    """Helper to collect status events for a duration.

    Args:
        nats_client: Connected NATS client
        timeout: How long to collect events (seconds)
        service_id: Optional service ID to filter
        subject_prefix: NATS subject prefix (default: "test.svc")

    Returns:
        List of collected status events
    """
    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=[f"{subject_prefix}.status.>"]
    )

    async with collector:
        await asyncio.sleep(timeout)

    if service_id:
        return collector.get_events(service_id=service_id)
    return collector.events


async def collect_heartbeat_events(
    nats_client: NATS,
    timeout: float = 5.0,
    service_id: str | None = None,
    subject_prefix: str = "test.svc"
) -> list[CollectedEvent]:
    """Helper to collect heartbeat events for a duration.

    Args:
        nats_client: Connected NATS client
        timeout: How long to collect events (seconds)
        service_id: Optional service ID to filter
        subject_prefix: NATS subject prefix (default: "test.svc")

    Returns:
        List of collected heartbeat events
    """
    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=[f"{subject_prefix}.heartbeat.>"]
    )

    async with collector:
        await asyncio.sleep(timeout)

    if service_id:
        return collector.get_events(service_id=service_id)
    return collector.events
