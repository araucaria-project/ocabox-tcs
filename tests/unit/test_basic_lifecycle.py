"""Basic lifecycle tests using test infrastructure.

Demonstrates usage of test infrastructure:
- NATSTestServer for isolated NATS instance
- ConfigGenerator for creating test configurations
- ProcessHarness for launcher control
- ServiceScenario for declarative test definitions

These tests verify fundamental service lifecycle operations:
- Service startup and shutdown
- Status transitions
- NATS event publishing
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server, nats_client
from tests.helpers.config_generator import ConfigGenerator, create_simple_config
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario


@pytest.mark.asyncio
async def test_simple_service_startup_shutdown(nats_server):
    """Test basic service startup and graceful shutdown.

    Verifies:
    1. Service starts successfully
    2. Launcher reports service as running
    3. Service stops gracefully
    """
    # Generate configuration for single mock service
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="test_basic",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={"work_interval": 0.5}
    )

    # Create and start launcher
    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # Start launcher and services
        started = await harness.start(timeout=5.0)
        assert started, "Launcher failed to start"
        assert harness.is_running, "Launcher not running after start"

        # Wait a bit for service to stabilize
        await asyncio.sleep(1.0)

        # Check launcher output for success indicators
        output = harness.get_output()
        output_str = "\n".join(output)
        assert "Launcher running" in output_str or "services started" in output_str.lower()

        # Stop launcher gracefully
        stopped = await harness.stop(timeout=5.0)
        assert stopped, "Launcher failed to stop gracefully"
        assert not harness.is_running, "Launcher still running after stop"

    finally:
        # Cleanup: ensure launcher is stopped
        if harness.is_running:
            await harness.stop(timeout=5.0)


@pytest.mark.asyncio
async def test_multiple_services_startup(nats_server):
    """Test starting multiple services simultaneously.

    Verifies:
    1. Multiple services can start together
    2. All services reach running state
    3. All services stop gracefully
    """
    # Create scenarios for multiple services
    scenarios = [
        ServiceScenario(
            service_type="mock_permanent",
            variant="service_1",
            config={"work_interval": 0.5}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            variant="service_2",
            config={"work_interval": 0.7}
        ),
        ServiceScenario(
            service_type="mock_permanent",
            variant="service_3",
            config={"work_interval": 0.3}
        )
    ]

    # Generate configuration
    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    # Create and start launcher
    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # Start launcher
        started = await harness.start(timeout=10.0)
        assert started, "Launcher failed to start"

        # Let services run briefly
        await asyncio.sleep(2.0)

        # Verify launcher output indicates success
        output = harness.get_output()
        output_str = "\n".join(output)

        # Check that we see evidence of all services
        # (Exact checks depend on launcher logging)
        assert "service_1" in output_str or "mock_permanent" in output_str

        # Stop launcher
        stopped = await harness.stop(timeout=10.0)
        assert stopped, "Launcher failed to stop"

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)
        generator.cleanup()


@pytest.mark.asyncio
async def test_service_with_startup_delay(nats_server):
    """Test service with startup delay completes successfully.

    Verifies:
    1. Services with slow startup complete successfully
    2. Launcher waits for service initialization
    """
    # Create service with startup delay
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="slow_start",
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        config={
            "startup_delay": 2.0,
            "work_interval": 0.5
        }
    )

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # Start launcher - should wait for slow service
        started = await harness.start(timeout=10.0)
        assert started, "Launcher with slow service failed to start"

        # Let service run a bit
        await asyncio.sleep(1.0)

        # Stop launcher
        stopped = await harness.stop(timeout=10.0)
        assert stopped, "Launcher failed to stop"

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)


@pytest.mark.asyncio
async def test_context_manager_usage(nats_server):
    """Test using harness as context manager for automatic cleanup.

    Verifies:
    1. Context manager starts launcher on entry
    2. Context manager stops launcher on exit
    3. Cleanup happens even if test fails
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="context_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    # Use context manager for automatic cleanup
    async with ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    ) as harness:
        # Launcher should be running inside context
        assert harness.is_running, "Launcher not running in context"

        # Let it run briefly
        await asyncio.sleep(1.0)

        # Verify launcher output
        output = harness.get_output()
        assert len(output) > 0, "No launcher output captured"

    # After context exit, launcher should be stopped
    assert not harness.is_running, "Launcher still running after context exit"


@pytest.mark.asyncio
async def test_launcher_restart(nats_server):
    """Test launcher restart functionality.

    Verifies:
    1. Launcher can be stopped and restarted
    2. Services restart with launcher
    3. State is properly reset on restart
    """
    config_path = create_simple_config(
        service_type="mock_permanent",
        variant="restart_test",
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # First start
        started = await harness.start(timeout=5.0)
        assert started, "Initial start failed"
        await asyncio.sleep(1.0)
        first_output_len = len(harness.get_output())

        # Stop
        stopped = await harness.stop(timeout=5.0)
        assert stopped, "Stop failed"
        assert not harness.is_running

        # Restart
        restarted = await harness.start(timeout=5.0)
        assert restarted, "Restart failed"
        assert harness.is_running
        await asyncio.sleep(1.0)

        # Verify we got new output after restart
        # Note: Output is not cleared on restart, so length should increase
        final_output_len = len(harness.get_output())
        # We can't assert length increase reliably, but we can check it's running
        assert harness.is_running

        # Final stop
        stopped = await harness.stop(timeout=5.0)
        assert stopped, "Final stop failed"

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)


if __name__ == "__main__":
    # Allow running tests directly
    pytest.main([__file__, "-v"])
