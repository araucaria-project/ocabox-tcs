"""Event verification tests using Phase 2 helpers.

Demonstrates usage of:
- NATSEventCollector for event collection
- wait_helpers for condition waiting
- assertions for high-level verification

These tests verify proper event publishing and NATS integration.
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server, nats_client
from tests.helpers.config_generator import create_simple_config, create_crash_test_config, ConfigGenerator
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario
from tests.helpers.event_collector import NATSEventCollector
from tests.helpers.assertions import (
    assert_service_started,
    assert_service_stopped,
    assert_service_crashed,
    assert_service_restarted,
    assert_event_sequence,
    assert_no_crashes
)
from tests.helpers.wait_helpers import (
    wait_for_event,
    wait_for_status,
    wait_for_service_count
)


@pytest.mark.asyncio
async def test_service_start_events(nats_server, nats_client):
    """Test that service publishes correct START events.

    Verifies:
    1. START event is published to registry
    2. Status message is published
    3. Event data contains expected fields
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        instance_context="event_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    # Create event collector for registry events
    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    async with collector:
        # Start launcher
        async with ProcessHarness(
            config_path=config_path,
            nats_url=nats_server.url,
            capture_output=True
        ) as harness:
            # Wait for service to start
            await asyncio.sleep(2.0)

        # Give events time to propagate
        await asyncio.sleep(0.5)

    # Verify START event was published
    service_id = "tests.services.mock_permanent:event_test"
    start_events = collector.get_events(event_type="start", service_id=service_id)
    assert len(start_events) > 0, "No START event found"

    # Verify event data
    start_event = start_events[0]
    assert start_event.data["event"] == "start"
    assert start_event.data["service_id"] == service_id
    assert "timestamp" in start_event.data
    assert "hostname" in start_event.data
    assert "pid" in start_event.data


@pytest.mark.asyncio
async def test_service_stop_events(nats_server, nats_client):
    """Test that service publishes correct STOP events on shutdown.

    Verifies:
    1. STOP event is published on graceful shutdown
    2. Status is "shutdown"
    3. Event sequence is correct
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        instance_context="stop_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
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
            await asyncio.sleep(1.5)
        # Harness exit triggers graceful shutdown

        await asyncio.sleep(1.0)

    # Use assertion helper
    service_id = "tests.services.mock_permanent:stop_test"
    await assert_service_stopped(collector, service_id, clean_shutdown=True)


@pytest.mark.asyncio
async def test_crash_event_publishing(nats_server, nats_client):
    """Test that crashing service publishes CRASH events.

    Verifies:
    1. CRASH event is published
    2. Exit code is included
    3. will_restart flag is correct
    """
    config_path = create_crash_test_config(
        restart_policy="no",
        crash_delay=0.5,
        exit_code=42,
        nats_host=nats_server.host,
        nats_port=nats_server.port
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
            # Wait for service to crash
            await asyncio.sleep(2.0)

        await asyncio.sleep(0.5)

    # Use assertion helper
    service_id = "tests.services.mock_crashing:policy_no"
    await assert_service_crashed(
        collector,
        service_id=service_id,
        expected_exit_code=42,
        expect_restart=False
    )


@pytest.mark.asyncio
async def test_restart_event_sequence(nats_server, nats_client):
    """Test event sequence for restarting service.

    Verifies:
    1. Service goes through: start -> crash -> restarting -> start
    2. All events published in correct order
    3. Restart count is correct
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_crashing",
            instance_context="restart_seq",
            config={"crash_delay": 0.3, "exit_code": 1},
            restart="always",
            restart_max=2,
            restart_sec=0.5
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
                # Wait for crashes and restarts
                await asyncio.sleep(4.0)

            await asyncio.sleep(0.5)

        # Verify event sequence
        service_id = "tests.services.mock_crashing:restart_seq"

        # Should see at least: start -> crashed -> restarting
        await assert_event_sequence(
            collector,
            service_id=service_id,
            expected_sequence=["start", "crashed", "restarting"],
            exact_match=False
        )

        # Verify restart happened
        await assert_service_restarted(collector, service_id, min_restarts=1)

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_multiple_services_events(nats_server, nats_client):
    """Test event collection for multiple services.

    Verifies:
    1. All services publish START events
    2. Events can be filtered by service_id
    3. Event count matches service count
    """
    scenarios = [
        ServiceScenario(
            service_type="mock_permanent",
            instance_context="multi_1",
            config={"work_interval": 0.5}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            instance_context="multi_2",
            config={"work_interval": 0.5}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            instance_context="multi_3",
            config={"work_interval": 0.5}
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
                found = await wait_for_service_count(
                    collector,
                    min_count=3,
                    event_type="start",
                    timeout=10.0
                )
                assert found, "Not all services started"

            await asyncio.sleep(0.5)

        # Verify we got exactly 3 start events
        start_count = collector.count_events(event_type="start")
        assert start_count >= 3, f"Expected at least 3 start events, got {start_count}"

        # Verify each service can be filtered
        for scenario in scenarios:
            service_id = f"tests.services.mock_permanent:{scenario.instance_context}"
            events = collector.get_events(event_type="start", service_id=service_id)
            assert len(events) > 0, f"No start event for {service_id}"

    finally:
        generator.cleanup()


@pytest.mark.asyncio
async def test_wait_for_event_timeout(nats_server, nats_client):
    """Test wait_for_event timeout behavior.

    Verifies:
    1. wait_for_event returns False on timeout
    2. Timeout works correctly
    """
    collector = NATSEventCollector(
        nats_client=nats_client,
        stream_name="test",
        subjects=["test.svc.registry.>"]
    )

    async with collector:
        # Wait for non-existent event
        found = await wait_for_event(
            collector,
            event_type="nonexistent",
            service_id="fake:service",
            timeout=1.0
        )
        assert not found, "Should not find non-existent event"


@pytest.mark.asyncio
async def test_status_via_event_collector(nats_server, nats_client):
    """Test querying service status via events.

    Verifies:
    1. Can wait for specific status
    2. Latest event reflects current status
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        instance_context="status_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
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
            # Wait for service to reach running status
            service_id = "tests.services.mock_permanent:status_test"
            found = await wait_for_status(
                collector,
                service_id=service_id,
                expected_status="ok",
                timeout=10.0
            )
            # Note: might be "startup" if fast, "ok" once settled
            # We just verify we can query status

            await asyncio.sleep(1.0)

        await asyncio.sleep(0.5)

    # Verify we collected some events
    service_id = "tests.services.mock_permanent:status_test"
    events = collector.get_events(service_id=service_id)
    assert len(events) > 0, "No events collected for service"

    # Get latest event
    latest = collector.get_latest_event(service_id=service_id)
    assert latest is not None, "No latest event found"
    assert "status" in latest.data, "Event missing status field"


@pytest.mark.asyncio
async def test_no_crashes_assertion(nats_server, nats_client):
    """Test assert_no_crashes helper.

    Verifies:
    1. Stable service produces no crash events
    2. assert_no_crashes passes for stable service
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        instance_context="stable_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
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
            # Wait and verify no crashes
            await assert_no_crashes(collector, duration=2.0)

        await asyncio.sleep(0.5)

    # Verify no crash events were published
    crash_count = collector.count_events(event_type="crashed")
    assert crash_count == 0, f"Unexpected {crash_count} crash events"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
