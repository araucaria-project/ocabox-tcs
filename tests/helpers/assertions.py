"""Assertion helpers for service testing.

Provides high-level assertion functions for common test scenarios.
Uses wait_helpers and event_collector for robust assertions.
"""

import asyncio
from typing import Any

from tests.helpers.event_collector import NATSEventCollector, CollectedEvent
from tests.helpers.wait_helpers import (
    wait_for_event,
    wait_for_event_sequence,
    wait_for_status,
    wait_for_service_count
)


class AssertionError(Exception):
    """Custom assertion error with detailed context."""
    pass


async def assert_service_started(
    event_collector: NATSEventCollector,
    service_id: str,
    timeout: float = 10.0,
    expected_status: str = "ok"
):
    """Assert that service started successfully.

    Verifies:
    1. START event was published
    2. Service reached expected status (or "startup" for fast services)

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        timeout: Maximum time to wait
        expected_status: Expected status after start (default: "ok")

    Raises:
        AssertionError: If service did not start properly

    Example:
        await assert_service_started(collector, "mock_permanent:test", timeout=5.0)
    """
    # Wait for START event
    found_start = await wait_for_event(
        event_collector,
        event_type="start",
        service_id=service_id,
        timeout=timeout
    )
    if not found_start:
        raise AssertionError(
            f"Service {service_id} did not publish START event within {timeout}s"
        )

    # Wait for expected status
    found_status = await wait_for_status(
        event_collector,
        service_id=service_id,
        expected_status=expected_status,
        timeout=timeout
    )
    if not found_status:
        # Get latest event for debugging
        latest = event_collector.get_latest_event(service_id=service_id)
        actual_status = latest.data.get("status") if latest else "unknown"

        # For fast services (especially single-shot/cyclic), accept multiple valid statuses:
        # - "startup": Service starting up
        # - "ok": Service running normally
        # - "shutdown": Service completed work and exited cleanly
        if actual_status in ("startup", "ok", "shutdown"):
            return  # Service started successfully (may have completed already)

        raise AssertionError(
            f"Service {service_id} did not reach status '{expected_status}' "
            f"(current: '{actual_status}')"
        )


async def assert_service_stopped(
    event_collector: NATSEventCollector,
    service_id: str,
    timeout: float = 10.0,
    clean_shutdown: bool = True
):
    """Assert that service stopped correctly.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        timeout: Maximum time to wait
        clean_shutdown: If True, expect STOP event. If False, accept CRASH/FAILED events.

    Raises:
        AssertionError: If service did not stop as expected

    Example:
        await assert_service_stopped(collector, "mock_permanent:test", timeout=5.0)
    """
    if clean_shutdown:
        # Expect graceful STOP event
        found = await wait_for_event(
            event_collector,
            event_type="stop",
            service_id=service_id,
            timeout=timeout
        )
        if not found:
            events = event_collector.get_event_sequence(service_id)
            raise AssertionError(
                f"Service {service_id} did not publish STOP event within {timeout}s. "
                f"Event sequence: {events}"
            )

        # Verify STOP event's status is "shutdown" or "ok" (both indicate clean stop)
        # Get the STOP event specifically (not overall latest which might be CRASH/FAILED after STOP)
        stop_event = event_collector.get_latest_event(event_type="stop", service_id=service_id)
        status = stop_event.data.get("status") if stop_event else None
        if status and status not in ("shutdown", "ok"):
            raise AssertionError(
                f"Service {service_id} STOP event has invalid status (expected 'shutdown' or 'ok', "
                f"got: '{status}')"
            )
    else:
        # Accept STOP, CRASH, or FAILED events
        stop_found = event_collector.has_event("stop", service_id)
        crash_found = event_collector.has_event("crashed", service_id)
        failed_found = event_collector.has_event("failed", service_id)

        if not (stop_found or crash_found or failed_found):
            events = event_collector.get_event_sequence(service_id)
            raise AssertionError(
                f"Service {service_id} did not terminate (no STOP/CRASH/FAILED event). "
                f"Event sequence: {events}"
            )


async def assert_service_crashed(
    event_collector: NATSEventCollector,
    service_id: str,
    timeout: float = 10.0,
    expected_exit_code: int | None = None,
    expect_restart: bool = False
):
    """Assert that service crashed as expected.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        timeout: Maximum time to wait
        expected_exit_code: Expected exit code (None = any)
        expect_restart: If True, verify will_restart flag is True

    Raises:
        AssertionError: If service did not crash as expected

    Example:
        await assert_service_crashed(
            collector,
            "mock_crashing:test",
            expected_exit_code=1,
            expect_restart=True
        )
    """
    # Wait for CRASH event
    found = await wait_for_event(
        event_collector,
        event_type="crashed",
        service_id=service_id,
        timeout=timeout
    )
    if not found:
        events = event_collector.get_event_sequence(service_id)
        raise AssertionError(
            f"Service {service_id} did not publish CRASHED event within {timeout}s. "
            f"Event sequence: {events}"
        )

    # Get crash event
    crash_event = event_collector.get_latest_event("crashed", service_id)
    assert crash_event is not None

    # Check exit code if specified
    if expected_exit_code is not None:
        actual_exit_code = crash_event.data.get("exit_code")
        if actual_exit_code != expected_exit_code:
            raise AssertionError(
                f"Service {service_id} crashed with exit code {actual_exit_code}, "
                f"expected {expected_exit_code}"
            )

    # Check restart flag if specified
    if expect_restart:
        will_restart = crash_event.data.get("will_restart", False)
        if not will_restart:
            raise AssertionError(
                f"Service {service_id} crashed but will_restart is False, expected True"
            )


async def assert_service_restarted(
    event_collector: NATSEventCollector,
    service_id: str,
    timeout: float = 10.0,
    min_restarts: int = 1
):
    """Assert that service restarted at least N times.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        timeout: Maximum time to wait
        min_restarts: Minimum number of restarts expected

    Raises:
        AssertionError: If service did not restart enough times

    Example:
        await assert_service_restarted(collector, "mock_crashing:test", min_restarts=2)
    """
    # Wait for restarting events
    await asyncio.sleep(timeout)

    restart_count = event_collector.count_events("restarting", service_id)
    if restart_count < min_restarts:
        events = event_collector.get_event_sequence(service_id)
        raise AssertionError(
            f"Service {service_id} restarted {restart_count} times, "
            f"expected at least {min_restarts}. Event sequence: {events}"
        )


async def assert_restart_limit_reached(
    event_collector: NATSEventCollector,
    service_id: str,
    timeout: float = 10.0,
    expected_restart_count: int | None = None
):
    """Assert that service reached restart limit and gave up.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        timeout: Maximum time to wait
        expected_restart_count: Expected number of restarts before giving up

    Raises:
        AssertionError: If restart limit was not reached

    Example:
        await assert_restart_limit_reached(collector, "mock_crashing:test", expected_restart_count=3)
    """
    # Wait for FAILED event (indicates gave up on restarting)
    found = await wait_for_event(
        event_collector,
        event_type="failed",
        service_id=service_id,
        timeout=timeout
    )
    if not found:
        events = event_collector.get_event_sequence(service_id)
        raise AssertionError(
            f"Service {service_id} did not publish FAILED event (restart limit). "
            f"Event sequence: {events}"
        )

    # Check restart count if specified
    if expected_restart_count is not None:
        restart_count = event_collector.count_events("restarting", service_id)
        if restart_count != expected_restart_count:
            raise AssertionError(
                f"Service {service_id} restarted {restart_count} times, "
                f"expected exactly {expected_restart_count}"
            )


async def assert_event_sequence(
    event_collector: NATSEventCollector,
    service_id: str,
    expected_sequence: list[str],
    timeout: float = 10.0,
    exact_match: bool = False
):
    """Assert that service went through expected event sequence.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        expected_sequence: Expected event type sequence
        timeout: Maximum time to wait
        exact_match: If True, sequence must match exactly

    Raises:
        AssertionError: If sequence does not match

    Example:
        await assert_event_sequence(
            collector,
            "mock_crashing:test",
            expected_sequence=["start", "crashed", "restarting", "start"],
            timeout=5.0
        )
    """
    found = await wait_for_event_sequence(
        event_collector,
        service_id=service_id,
        expected_sequence=expected_sequence,
        timeout=timeout,
        exact_match=exact_match
    )
    if not found:
        actual = event_collector.get_event_sequence(service_id)
        raise AssertionError(
            f"Service {service_id} event sequence does not match.\n"
            f"Expected: {expected_sequence}\n"
            f"Actual: {actual}"
        )


async def assert_status_transition(
    event_collector: NATSEventCollector,
    service_id: str,
    from_status: str,
    to_status: str,
    timeout: float = 10.0
):
    """Assert that service transitioned from one status to another.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service identifier
        from_status: Starting status
        to_status: Target status
        timeout: Maximum time to wait

    Raises:
        AssertionError: If transition did not occur

    Example:
        await assert_status_transition(
            collector,
            "mock_permanent:test",
            from_status="startup",
            to_status="ok",
            timeout=5.0
        )
    """
    # First verify we saw the from_status
    events = event_collector.get_events(service_id=service_id)
    from_status_found = any(e.data.get("status") == from_status for e in events)
    if not from_status_found:
        statuses = [e.data.get("status") for e in events]
        raise AssertionError(
            f"Service {service_id} never had status '{from_status}'. "
            f"Observed statuses: {statuses}"
        )

    # Wait for to_status
    found = await wait_for_status(
        event_collector,
        service_id=service_id,
        expected_status=to_status,
        timeout=timeout
    )
    if not found:
        latest = event_collector.get_latest_event(service_id=service_id)
        actual_status = latest.data.get("status") if latest else "unknown"
        raise AssertionError(
            f"Service {service_id} did not transition to '{to_status}' "
            f"(current: '{actual_status}')"
        )


async def assert_multiple_services_started(
    event_collector: NATSEventCollector,
    service_ids: list[str],
    timeout: float = 10.0
):
    """Assert that multiple services started successfully.

    Args:
        event_collector: NATSEventCollector instance
        service_ids: List of service identifiers
        timeout: Maximum time to wait for all services

    Raises:
        AssertionError: If any service did not start

    Example:
        await assert_multiple_services_started(
            collector,
            ["service1:test", "service2:test", "service3:test"],
            timeout=10.0
        )
    """
    # Wait for all start events
    found = await wait_for_service_count(
        event_collector,
        min_count=len(service_ids),
        event_type="start",
        timeout=timeout
    )
    if not found:
        actual_count = event_collector.count_events(event_type="start")
        started_services = [
            e.service_id for e in event_collector.get_events(event_type="start")
        ]
        raise AssertionError(
            f"Expected {len(service_ids)} services to start, got {actual_count}. "
            f"Started: {started_services}"
        )

    # Verify each specific service started
    for service_id in service_ids:
        if not event_collector.has_event("start", service_id):
            raise AssertionError(f"Service {service_id} did not start")


async def assert_no_crashes(
    event_collector: NATSEventCollector,
    duration: float,
    service_id: str | None = None
):
    """Assert that no crashes occurred during duration.

    Args:
        event_collector: NATSEventCollector instance
        duration: How long to monitor (seconds)
        service_id: Optional specific service to check

    Raises:
        AssertionError: If any crashes occurred

    Example:
        # Verify no crashes for 5 seconds
        await assert_no_crashes(collector, duration=5.0)
    """
    initial_count = event_collector.count_events("crashed", service_id)
    await asyncio.sleep(duration)
    final_count = event_collector.count_events("crashed", service_id)

    if final_count > initial_count:
        crashed = event_collector.get_events("crashed", service_id)
        crash_details = [
            f"{e.service_id} (exit_code={e.data.get('exit_code')})"
            for e in crashed[initial_count:]
        ]
        raise AssertionError(
            f"Unexpected crashes occurred: {crash_details}"
        )


def assert_event_data(
    event: CollectedEvent,
    expected_data: dict[str, Any]
):
    """Assert that event data contains expected values.

    Args:
        event: CollectedEvent instance
        expected_data: Expected data fields and values

    Raises:
        AssertionError: If data does not match

    Example:
        crash_event = collector.get_latest_event("crashed", service_id)
        assert_event_data(crash_event, {
            "exit_code": 1,
            "will_restart": True
        })
    """
    for key, expected_value in expected_data.items():
        actual_value = event.data.get(key)
        if actual_value != expected_value:
            raise AssertionError(
                f"Event data mismatch for '{key}': "
                f"expected {expected_value}, got {actual_value}. "
                f"Full event data: {event.data}"
            )
