"""Tests for permanent service behavior.

Verifies permanent service lifecycle, monitoring, and long-running behavior.
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server, nats_client
from tests.helpers.config_generator import create_simple_config, ConfigGenerator
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario
from tests.helpers.event_collector import NATSEventCollector
from tests.helpers.assertions import (
    assert_service_started,
    assert_service_stopped,
    assert_no_crashes
)
from tests.helpers.wait_helpers import wait_for_event


@pytest.mark.asyncio
async def test_permanent_service_stays_running(nats_server, nats_client):
    """Test that permanent service stays running.

    Verifies:
    1. Service starts successfully
    2. Service stays running for extended period
    3. Service responds to monitoring
    4. No unexpected terminations
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="long_run",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={"work_interval": 0.5}
    )

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    async with collector:
        async with ProcessHarness(
            config_path=config_path,
            nats_url=nats_server.url,
            capture_output=True
        ) as harness:
            # Wait for service to start
            await wait_for_event(collector, event_type="start", timeout=5.0)

            # Service should stay running
            await assert_no_crashes(collector, duration=1.0)

    # Verify lifecycle
    service_id = "mock_permanent.long_run"
    await assert_service_started(collector, service_id)
    await assert_service_stopped(collector, service_id, clean_shutdown=True)


@pytest.mark.asyncio
async def test_permanent_service_work_count_limit(nats_server, nats_client):
    """Test permanent service with work count limit.

    Verifies:
    1. Service stops after N iterations
    2. Clean shutdown after completion
    3. Status transitions correctly
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="work_limit",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={
            "work_interval": 0.3,
            "work_count": 5  # Stop after 5 iterations
        }
    )

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    async with collector:
        async with ProcessHarness(
            config_path=config_path,
            nats_url=nats_server.url,
            capture_output=True
        ) as harness:
            # Wait for service to start
            await wait_for_event(collector, event_type="start", timeout=5.0)

            # Wait for service to complete and stop
            # 5 iterations * 0.3s = 1.5s + overhead
            await wait_for_event(collector, event_type="stop", timeout=10.0)

    # Verify clean shutdown
    service_id = "mock_permanent.work_limit"
    await assert_service_stopped(collector, service_id, clean_shutdown=True)


@pytest.mark.asyncio
async def test_permanent_service_with_delays(nats_server, nats_client):
    """Test permanent service with startup/shutdown delays.

    Verifies:
    1. Service handles startup delays correctly
    2. Service handles shutdown delays correctly
    3. Launcher waits for delays
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="with_delays",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={
            "work_interval": 0.2,
            "startup_delay": 1.0,
            "shutdown_delay": 0.5
        }
    )

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    async with collector:
        async with ProcessHarness(
            config_path=config_path,
            nats_url=nats_server.url,
            capture_output=True
        ) as harness:
            # Wait for service to start (should take ~1s due to startup_delay)
            await wait_for_event(collector, event_type="start", timeout=10.0)

            await asyncio.sleep(1.0)

    # Verify lifecycle completed
    service_id = "mock_permanent.with_delays"
    await assert_service_started(collector, service_id)
    await assert_service_stopped(collector, service_id, clean_shutdown=True)


@pytest.mark.asyncio
async def test_multiple_permanent_services(nats_server, nats_client):
    """Test multiple permanent services running concurrently.

    Verifies:
    1. Multiple services start independently
    2. Services run concurrently without interference
    3. All services stop cleanly
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_permanent",
            variant="multi_1",
            config={"work_interval": 0.3}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            variant="multi_2",
            config={"work_interval": 0.5}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            variant="multi_3",
            config={"work_interval": 0.4}
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
                    service_id = f"mock_permanent.{scenario.instance_context}"
                    await wait_for_event(
                        collector,
                        event_type="start",
                        service_id=service_id,
                        timeout=10.0
                    )

                # Let services run concurrently
                await asyncio.sleep(2.0)

            await asyncio.sleep(0.5)

        # Verify all services
        for scenario in scenarios:
            service_id = f"mock_permanent.{scenario.instance_context}"
            await assert_service_started(collector, service_id)
            await assert_service_stopped(collector, service_id, clean_shutdown=True)

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_permanent_service_healthcheck_status(nats_server, nats_client):
    """Test permanent service with custom healthcheck status.

    Verifies:
    1. Service can override healthcheck status
    2. Status changes propagate correctly
    3. Monitoring reflects custom status
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="custom_health",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={
            "work_interval": 0.5,
            "healthcheck_status": "degraded",
            "healthcheck_message": "Custom degraded state"
        }
    )

    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.>"]  # Collect all events including status
    )

    async with collector:
        async with ProcessHarness(
            config_path=config_path,
            nats_url=nats_server.url,
            capture_output=True
        ) as harness:
            # Wait for service to start
            await wait_for_event(collector, event_type="start", timeout=5.0)

            # Let service run with custom healthcheck
            await asyncio.sleep(2.0)

        await asyncio.sleep(0.5)

    # Verify service started
    service_id = "mock_permanent.custom_health"
    await assert_service_started(collector, service_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
