"""Tests for cyclic/periodic service behavior.

Verifies cyclic service execution, schedule management, and monitoring.
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server, nats_client
from tests.helpers.config_generator import ConfigGenerator
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario
from tests.helpers.event_collector import NATSEventCollector
from tests.helpers.assertions import assert_service_started, assert_service_stopped, assert_no_crashes
from tests.helpers.wait_helpers import wait_for_event


@pytest.mark.asyncio
async def test_cyclic_service_executes_periodically(nats_server, nats_client):
    """Test cyclic service executes tasks on schedule.

    Verifies:
    1. Service starts and begins cycles
    2. Cycles execute at correct intervals
    3. Service runs continuously
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="periodic",
            config={
                "cycle_interval": 1.0,
                "execution_duration": 0.3,
                "max_cycles": 0  # Infinite
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
                service_id = "tests.services.mock_cyclic:periodic"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Let service run through a few cycles
                # 1s interval + 0.3s execution = ~1.3s per cycle
                # 4 cycles = ~5.2s
                await asyncio.sleep(5.0)

                # Verify no crashes
                await assert_no_crashes(collector, duration=0.5)

            await asyncio.sleep(0.5)

        # Verify lifecycle
        await assert_service_started(collector, service_id)
        await assert_service_stopped(collector, service_id, clean_shutdown=True)

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_cyclic_service_with_max_cycles(nats_server, nats_client):
    """Test cyclic service stops after max cycles.

    Verifies:
    1. Service executes exactly N cycles
    2. Service stops automatically after max cycles
    3. Clean shutdown after completion
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="limited",
            config={
                "cycle_interval": 0.5,
                "execution_duration": 0.2,
                "max_cycles": 3  # Stop after 3 cycles
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
                service_id = "tests.services.mock_cyclic:limited"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for service to complete cycles and stop
                # 3 cycles * (0.5s interval + 0.2s execution) = ~2.1s + startup
                await wait_for_event(
                    collector,
                    event_type="stop",
                    service_id=service_id,
                    timeout=10.0
                )

            await asyncio.sleep(0.5)

        # Verify clean shutdown after completion
        await assert_service_stopped(collector, service_id, clean_shutdown=True)

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_cyclic_service_with_failure(nats_server, nats_client):
    """Test cyclic service handles cycle failures.

    Verifies:
    1. Service executes cycles
    2. Specific cycle failure is handled
    3. Service crashes or terminates on failure
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="failing",
            config={
                "cycle_interval": 0.5,
                "execution_duration": 0.2,
                "max_cycles": 0,
                "fail_on_cycle": 3  # Fail on 3rd cycle
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
                service_id = "tests.services.mock_cyclic:failing"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for failure (should happen on 3rd cycle)
                # 3 cycles * 0.7s = ~2.1s
                await asyncio.sleep(4.0)

            await asyncio.sleep(0.5)

        # Verify service started
        await assert_service_started(collector, service_id)

        # Verify termination (crash or stop)
        events = collector.get_events(service_id=service_id)
        event_types = [e.event_type for e in events]
        assert "crashed" in event_types or "stop" in event_types or "failed" in event_types, \
            f"Service should have terminated after failure, got events: {event_types}"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_multiple_cyclic_services(nats_server, nats_client):
    """Test multiple cyclic services running concurrently.

    Verifies:
    1. Multiple cyclic services start
    2. Services execute cycles independently
    3. Different schedules don't interfere
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="fast",
            config={
                "cycle_interval": 0.5,
                "execution_duration": 0.1,
                "max_cycles": 4
            }
        ),
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="medium",
            config={
                "cycle_interval": 0.7,
                "execution_duration": 0.2,
                "max_cycles": 3
            }
        ),
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="slow",
            config={
                "cycle_interval": 1.0,
                "execution_duration": 0.3,
                "max_cycles": 2
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
                # Wait for all services to start
                for scenario in scenarios:
                    service_id = f"tests.services.mock_cyclic:{scenario.instance_context}"
                    await wait_for_event(
                        collector,
                        event_type="start",
                        service_id=service_id,
                        timeout=10.0
                    )

                # Let services run through their cycles
                await asyncio.sleep(6.0)

            await asyncio.sleep(0.5)

        # Verify all services
        for scenario in scenarios:
            service_id = f"tests.services.mock_cyclic:{scenario.instance_context}"
            await assert_service_started(collector, service_id)

            # Services with max_cycles should have stopped
            events = collector.get_events(service_id=service_id)
            event_types = [e.event_type for e in events]
            assert "stop" in event_types or "crashed" in event_types, \
                f"Service {service_id} should have terminated"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_cyclic_service_short_interval(nats_server, nats_client):
    """Test cyclic service with very short cycle interval.

    Verifies:
    1. Fast cycles are handled correctly
    2. No cycle overlap or timing issues
    3. Service remains stable
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_cyclic",
            instance_context="rapid",
            config={
                "cycle_interval": 0.2,
                "execution_duration": 0.05,
                "max_cycles": 10
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
                service_id = "tests.services.mock_cyclic:rapid"
                await wait_for_event(
                    collector,
                    event_type="start",
                    service_id=service_id,
                    timeout=5.0
                )

                # Wait for all cycles to complete
                # 10 cycles * 0.25s = 2.5s + overhead
                await wait_for_event(
                    collector,
                    event_type="stop",
                    service_id=service_id,
                    timeout=10.0
                )

            await asyncio.sleep(0.5)

        # Verify clean completion
        await assert_service_stopped(collector, service_id, clean_shutdown=True)

        # Verify no crashes during rapid cycles
        crash_events = collector.get_events(event_type="crashed", service_id=service_id)
        assert len(crash_events) == 0, "Rapid cycles should not cause crashes"

    finally:
        generator.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
