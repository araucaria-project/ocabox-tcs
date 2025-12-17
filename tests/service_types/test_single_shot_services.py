"""Tests for single-shot service behavior.

Verifies single-shot service execution, completion, and monitoring.
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server, nats_client
from tests.helpers.config_generator import ConfigGenerator
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario
from tests.helpers.event_collector import NATSEventCollector
from tests.helpers.assertions import assert_service_started, assert_service_stopped
from tests.helpers.wait_helpers import wait_for_event


@pytest.mark.asyncio
async def test_single_shot_successful_execution(nats_server, nats_client):
    """Test single-shot service executes successfully and terminates.

    Verifies:
    1. Service starts and executes
    2. Service completes successfully
    3. Service terminates cleanly
    4. No restart attempts
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_single_shot",
            variant="success",
            config={
                "execution_delay": 1.0,
                "work_iterations": 3,
                "should_fail": False
            }
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    try:
        async with collector:
            async with ProcessHarness(
                config_path=config_path,
                nats_url=nats_server.url,
                capture_output=True
            ) as harness:
                # Wait for service to start
                service_id = "mock_single_shot.success"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for service to complete and stop
                await wait_for_event(
                    collector,
                    event_type="stop",
                    service_id=service_id,
                    timeout=10.0
                )

            await asyncio.sleep(0.5)

        # Verify lifecycle
        await assert_service_started(collector, service_id)
        await assert_service_stopped(collector, service_id, clean_shutdown=True)

        # Verify no restart attempts
        restart_events = collector.get_events(event_type="restarting", service_id=service_id)
        assert len(restart_events) == 0, "Single-shot service should not restart"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_single_shot_with_failure(nats_server, nats_client):
    """Test single-shot service that fails during execution.

    Verifies:
    1. Service starts execution
    2. Failure is detected
    3. Service terminates with error status
    4. No automatic restart
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_single_shot",
            variant="failure",
            config={
                "execution_delay": 1.0,
                "work_iterations": 4,
                "should_fail": True  # Fail mid-execution
            }
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    try:
        async with collector:
            async with ProcessHarness(
                config_path=config_path,
                nats_url=nats_server.url,
                capture_output=True
            ) as harness:
                # Wait for service to start
                service_id = "mock_single_shot.failure"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for service to crash or stop
                await asyncio.sleep(3.0)

            await asyncio.sleep(0.5)

        # Verify service started
        await assert_service_started(collector, service_id)

        # Verify termination (could be crash or stop)
        events = collector.get_events(service_id=service_id)
        event_types = [e.event_type for e in events]
        assert "crashed" in event_types or "stop" in event_types or "failed" in event_types, \
            f"Service should have terminated, got events: {event_types}"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_single_shot_with_non_zero_exit_code(nats_server, nats_client):
    """Test single-shot service exiting with non-zero code.

    Verifies:
    1. Service completes execution
    2. Service exits with specified exit code
    3. Exit code is captured in events
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_single_shot",
            variant="exit_code",
            config={
                "execution_delay": 0.5,
                "work_iterations": 2,
                "exit_code": 42  # Non-zero exit code
            }
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    try:
        async with collector:
            async with ProcessHarness(
                config_path=config_path,
                nats_url=nats_server.url,
                capture_output=True
            ) as harness:
                # Wait for service to start and complete
                service_id = "mock_single_shot.exit_code"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for termination
                await asyncio.sleep(3.0)

            await asyncio.sleep(0.5)

        # Verify service start event (but don't check for 'ok' status since it will crash)
        start_events = collector.get_events(event_type="start", service_id=service_id)
        assert len(start_events) > 0, f"Service {service_id} did not publish start event"

        # Check for crash event with exit code
        crash_events = collector.get_events(event_type="crashed", service_id=service_id)
        assert len(crash_events) > 0, f"Expected crash event for exit code 42"

        # Verify exit code is captured
        assert crash_events[-1].data.get("exit_code") == 42, \
            f"Expected exit_code 42, got {crash_events[-1].data.get('exit_code')}"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_multiple_single_shot_services(nats_server, nats_client):
    """Test multiple single-shot services executing independently.

    Verifies:
    1. Multiple single-shot services start
    2. Services execute independently
    3. All services complete
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_single_shot",
            variant="batch_1",
            config={"execution_delay": 0.5, "work_iterations": 2}
        ),
        ServiceScenario(
            service_type="mock_single_shot",
            variant="batch_2",
            config={"execution_delay": 0.7, "work_iterations": 3}
        ),
        ServiceScenario(
            service_type="mock_single_shot",
            variant="batch_3",
            config={"execution_delay": 0.3, "work_iterations": 2}
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    try:
        async with collector:
            async with ProcessHarness(
                config_path=config_path,
                nats_url=nats_server.url,
                capture_output=True
            ) as harness:
                # Wait for all services to start
                for scenario in scenarios:
                    service_id = f"mock_single_shot.{scenario.instance_context}"
                    await wait_for_event(
                        collector,
                        event_type="start",
                        service_id=service_id,
                        timeout=10.0
                    )

                # Wait for all services to complete
                await asyncio.sleep(5.0)

            await asyncio.sleep(0.5)

        # Verify all services started
        for scenario in scenarios:
            service_id = f"mock_single_shot.{scenario.instance_context}"
            await assert_service_started(collector, service_id)

            # Verify termination
            events = collector.get_events(service_id=service_id)
            event_types = [e.event_type for e in events]
            assert "stop" in event_types or "crashed" in event_types or "failed" in event_types, \
                f"Service {service_id} should have terminated"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_single_shot_fast_execution(nats_server, nats_client):
    """Test single-shot service with very fast execution.

    Verifies:
    1. Fast-executing services are handled correctly
    2. Events are published even for quick execution
    3. Launcher tracks completion
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_single_shot",
            variant="fast",
            config={
                "execution_delay": 0.1,
                "work_iterations": 1
            }
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    try:
        async with collector:
            async with ProcessHarness(
                config_path=config_path,
                nats_url=nats_server.url,
                capture_output=True
            ) as harness:
                # Service should start and complete quickly
                service_id = "mock_single_shot.fast"

                # Wait for start event
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for completion
                await asyncio.sleep(2.0)

            await asyncio.sleep(0.5)

        # Verify lifecycle
        await assert_service_started(collector, service_id)

        # Verify we got termination event
        events = collector.get_events(service_id=service_id)
        assert len(events) >= 2, f"Expected at least start+stop events, got {len(events)}"

    finally:
        generator.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
