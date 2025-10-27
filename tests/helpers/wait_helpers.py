"""Wait and condition helpers for tests.

Provides utilities for waiting for conditions with timeout.
Reduces flakiness from fixed sleep times in tests.
"""

import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


async def wait_for_condition(
    condition: Callable[[], bool] | Callable[[], Awaitable[bool]],
    timeout: float = 10.0,
    poll_interval: float = 0.1,
    error_message: str = "Condition not met within timeout"
) -> bool:
    """Wait for a condition to become true.

    Args:
        condition: Function that returns True when condition is met
                  Can be sync or async function
        timeout: Maximum time to wait in seconds
        poll_interval: Time between condition checks in seconds
        error_message: Error message if timeout is reached

    Returns:
        True if condition met, False if timeout

    Example:
        await wait_for_condition(
            lambda: service.status == "running",
            timeout=5.0,
            error_message="Service did not start"
        )
    """
    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            # Check if condition is async or sync
            if asyncio.iscoroutinefunction(condition):
                result = await condition()
            else:
                result = condition()

            if result:
                return True
        except Exception as e:
            logger.debug(f"Condition check raised exception: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"{error_message} (timeout after {timeout}s)")
    return False


async def wait_for_event(
    event_collector,
    event_type: str | None = None,
    service_id: str | None = None,
    status: str | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.1
) -> bool:
    """Wait for specific event to appear in collector.

    Args:
        event_collector: NATSEventCollector instance
        event_type: Event type to wait for (e.g., "start", "crash")
        service_id: Service ID to match
        status: Status to match
        timeout: Maximum time to wait in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if event found, False if timeout

    Example:
        async with NATSEventCollector(...) as collector:
            found = await wait_for_event(
                collector,
                event_type="start",
                service_id="mock_permanent:test",
                timeout=5.0
            )
            assert found, "Service start event not received"
    """
    return await wait_for_condition(
        lambda: event_collector.has_event(event_type, service_id, status),
        timeout=timeout,
        poll_interval=poll_interval,
        error_message=f"Event not found: type={event_type}, service={service_id}, status={status}"
    )


async def wait_for_event_sequence(
    event_collector,
    service_id: str,
    expected_sequence: list[str],
    timeout: float = 10.0,
    poll_interval: float = 0.1,
    exact_match: bool = False
) -> bool:
    """Wait for specific event sequence to occur.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service ID to check
        expected_sequence: Expected sequence of event types
        timeout: Maximum time to wait in seconds
        poll_interval: Time between checks in seconds
        exact_match: If True, sequence must match exactly
                    If False, expected events must appear in order (but can have extras)

    Returns:
        True if sequence found, False if timeout

    Example:
        # Wait for service to go through: start -> crash -> restarting -> start
        found = await wait_for_event_sequence(
            collector,
            service_id="mock_crashing:test",
            expected_sequence=["start", "crashed", "restarting", "start"],
            timeout=5.0
        )
    """
    def check_sequence():
        actual = event_collector.get_event_sequence(service_id)
        if exact_match:
            return actual == expected_sequence
        else:
            # Check if expected events appear in order (subsequence)
            expected_idx = 0
            for event_type in actual:
                if expected_idx < len(expected_sequence) and event_type == expected_sequence[expected_idx]:
                    expected_idx += 1
            return expected_idx == len(expected_sequence)

    return await wait_for_condition(
        check_sequence,
        timeout=timeout,
        poll_interval=poll_interval,
        error_message=f"Event sequence not found for {service_id}: expected {expected_sequence}"
    )


async def wait_for_status(
    event_collector,
    service_id: str,
    expected_status: str,
    timeout: float = 10.0,
    poll_interval: float = 0.1
) -> bool:
    """Wait for service to reach specific status.

    Args:
        event_collector: NATSEventCollector instance
        service_id: Service ID to check
        expected_status: Expected status value (e.g., "ok", "failed")
        timeout: Maximum time to wait in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if status reached, False if timeout

    Example:
        found = await wait_for_status(
            collector,
            service_id="mock_permanent:test",
            expected_status="ok",
            timeout=5.0
        )
    """
    def check_status():
        # Get latest status or registry event for service
        latest_status = event_collector.get_latest_event(service_id=service_id)
        if latest_status:
            return latest_status.data.get("status") == expected_status
        return False

    return await wait_for_condition(
        check_status,
        timeout=timeout,
        poll_interval=poll_interval,
        error_message=f"Service {service_id} did not reach status {expected_status}"
    )


async def wait_for_launcher_ready(
    harness,
    timeout: float = 10.0,
    poll_interval: float = 0.1
) -> bool:
    """Wait for launcher to become ready.

    Args:
        harness: LauncherHarness instance
        timeout: Maximum time to wait in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if launcher ready, False if timeout

    Example:
        harness = ProcessHarness(...)
        await harness.start()
        ready = await wait_for_launcher_ready(harness, timeout=5.0)
        assert ready, "Launcher did not become ready"
    """
    return await wait_for_condition(
        lambda: harness.is_running,
        timeout=timeout,
        poll_interval=poll_interval,
        error_message="Launcher did not become ready"
    )


async def wait_for_service_count(
    event_collector,
    min_count: int,
    event_type: str = "start",
    timeout: float = 10.0,
    poll_interval: float = 0.1
) -> bool:
    """Wait for minimum number of service events.

    Useful for waiting for multiple services to start.

    Args:
        event_collector: NATSEventCollector instance
        min_count: Minimum number of events to wait for
        event_type: Event type to count (default: "start")
        timeout: Maximum time to wait in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if count reached, False if timeout

    Example:
        # Wait for 3 services to start
        found = await wait_for_service_count(
            collector,
            min_count=3,
            event_type="start",
            timeout=10.0
        )
    """
    return await wait_for_condition(
        lambda: event_collector.count_events(event_type=event_type) >= min_count,
        timeout=timeout,
        poll_interval=poll_interval,
        error_message=f"Did not see {min_count} '{event_type}' events"
    )


async def wait_for_no_events(
    event_collector,
    duration: float,
    event_type: str | None = None,
    service_id: str | None = None
) -> bool:
    """Wait and verify no events occur during duration.

    Useful for verifying service stays in stable state.

    Args:
        event_collector: NATSEventCollector instance
        duration: How long to monitor (seconds)
        event_type: Optional event type to check
        service_id: Optional service ID to check

    Returns:
        True if no matching events occurred, False if events found

    Example:
        # Verify no crashes for 5 seconds
        stable = await wait_for_no_events(
            collector,
            duration=5.0,
            event_type="crashed"
        )
        assert stable, "Service crashed unexpectedly"
    """
    initial_count = event_collector.count_events(event_type, service_id)
    await asyncio.sleep(duration)
    final_count = event_collector.count_events(event_type, service_id)
    return initial_count == final_count


class WaitTimeout(Exception):
    """Exception raised when wait condition times out."""
    pass


async def wait_for_condition_strict(
    condition: Callable[[], bool] | Callable[[], Awaitable[bool]],
    timeout: float = 10.0,
    poll_interval: float = 0.1,
    error_message: str = "Condition not met within timeout"
):
    """Wait for condition, raise exception on timeout.

    Same as wait_for_condition but raises WaitTimeout instead of returning False.

    Args:
        condition: Function that returns True when condition is met
        timeout: Maximum time to wait in seconds
        poll_interval: Time between condition checks in seconds
        error_message: Error message if timeout is reached

    Raises:
        WaitTimeout: If condition not met within timeout

    Example:
        await wait_for_condition_strict(
            lambda: service.is_ready,
            timeout=5.0,
            error_message="Service did not become ready"
        )
    """
    result = await wait_for_condition(
        condition=condition,
        timeout=timeout,
        poll_interval=poll_interval,
        error_message=error_message
    )
    if not result:
        raise WaitTimeout(error_message)


async def retry_until_success(
    operation: Callable[[], Awaitable[Any]],
    max_attempts: int = 3,
    delay: float = 1.0,
    error_message: str = "Operation failed after retries"
) -> Any:
    """Retry async operation until success or max attempts.

    Args:
        operation: Async function to retry
        max_attempts: Maximum number of attempts
        delay: Delay between attempts in seconds
        error_message: Error message if all attempts fail

    Returns:
        Result from operation

    Raises:
        Exception: Last exception if all attempts fail

    Example:
        result = await retry_until_success(
            lambda: harness.start(),
            max_attempts=3,
            delay=1.0
        )
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as e:
            last_error = e
            logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(delay)

    raise Exception(f"{error_message}: {last_error}")
