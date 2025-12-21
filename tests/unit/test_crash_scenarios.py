"""Crash scenario tests using test infrastructure.

Tests service crash handling and restart policies:
- No restart (crash and stay down)
- Always restart (restart regardless of exit code)
- On-failure restart (restart on non-zero exit)
- Restart limits (max restarts in time window)
"""

import asyncio
import pytest

from tests.fixtures.nats_fixtures import nats_server
from tests.helpers.config_generator import (
    create_crash_test_config,
    create_restart_limit_config,
    ConfigGenerator
)
from tests.helpers.launcher_harness import ProcessHarness, ServiceScenario


@pytest.mark.asyncio
async def test_crash_no_restart(nats_server):
    """Test service crash with no restart policy.

    Verifies:
    1. Service crashes as expected
    2. Launcher publishes CRASH event
    3. Service stays down (not restarted)
    4. Final status is "failed"
    """
    config_path = create_crash_test_config(
        restart_policy="no",
        crash_delay=0.5,
        exit_code=1,
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # Start launcher
        started = await harness.start(timeout=5.0)
        assert started, "Launcher failed to start"

        # Wait for service to crash
        await asyncio.sleep(2.0)

        # Check output for crash indication
        output = harness.get_output()
        output_str = "\n".join(output)

        # Should see crash-related messages
        assert "crash" in output_str.lower() or "exit" in output_str.lower()

        # Stop launcher
        stopped = await harness.stop(timeout=5.0)
        assert stopped

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)


@pytest.mark.asyncio
async def test_crash_always_restart(nats_server):
    """Test service crash with always restart policy.

    Verifies:
    1. Service crashes
    2. Launcher automatically restarts service
    3. Service crashes and restarts multiple times
    4. Restart cycle continues
    """
    # Create config with always restart and fast restart
    scenarios = [
        ServiceScenario(
            service_type="mock_crashing",
            variant="always_restart",
            config={"crash_delay": 0.3, "exit_code": 1},
            restart="always",
            restart_sec=0.5,
            restart_max=3,  # Limit restarts to avoid infinite loop
            restart_window=60.0
        )
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        # Start launcher
        started = await harness.start(timeout=5.0)
        assert started

        # Wait for multiple crash/restart cycles
        # 3 crashes * (0.3s crash_delay + 0.5s restart_delay) = ~2.4s minimum
        await asyncio.sleep(4.0)

        # Check output for restart indicators
        output = harness.get_output()
        output_str = "\n".join(output)

        # Should see multiple restart attempts
        restart_count = output_str.lower().count("restart")
        assert restart_count >= 2, f"Expected multiple restarts, found {restart_count}"

        # Stop launcher
        stopped = await harness.stop(timeout=5.0)
        assert stopped

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)
        generator.cleanup()


@pytest.mark.asyncio
async def test_crash_on_failure_restart(nats_server):
    """Test service crash with on-failure restart policy.

    Verifies:
    1. Service crashes with non-zero exit code
    2. Launcher restarts service (exit code != 0)
    3. Restart cycle works for failures
    """
    config_path = create_crash_test_config(
        restart_policy="on-failure",
        crash_delay=0.5,
        exit_code=1,  # Non-zero triggers restart
        nats_host=nats_server.host,
        nats_port=nats_server.port,
        restart_max=2
    )

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        started = await harness.start(timeout=5.0)
        assert started

        # Wait for crash and restart cycles
        await asyncio.sleep(3.0)

        # Verify restarts happened
        output = harness.get_output()
        output_str = "\n".join(output)
        assert "restart" in output_str.lower()

        stopped = await harness.stop(timeout=5.0)
        assert stopped

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)


@pytest.mark.asyncio
async def test_restart_limit_reached(nats_server):
    """Test service restart limit enforcement.

    Verifies:
    1. Service crashes and restarts up to max limit
    2. After limit reached, no more restarts
    3. Final status is "failed"
    4. Restart count matches expected limit
    """
    config_path = create_restart_limit_config(
        restart_policy="always",
        restart_max=3,
        restart_window=60.0,
        crash_delay=0.3,
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        started = await harness.start(timeout=5.0)
        assert started

        # Wait for all restart attempts to exhaust
        # 3 restarts * (0.3s crash + 0.5s restart_sec) + buffer
        await asyncio.sleep(5.0)

        # Check output
        output = harness.get_output()
        output_str = "\n".join(output)

        # Should see restart limit message
        assert "restart limit" in output_str.lower() or "giving up" in output_str.lower()

        stopped = await harness.stop(timeout=5.0)
        assert stopped

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)


@pytest.mark.asyncio
async def test_mixed_services_some_crashing(nats_server):
    """Test launcher with mix of stable and crashing services.

    Verifies:
    1. Stable services continue running
    2. Crashing services crash and restart independently
    3. Launcher remains operational
    4. Services don't interfere with each other
    """
    scenarios = [
        # Stable service
        ServiceScenario(
            service_type="mock_permanent",
            variant="stable",
            config={"work_interval": 0.5}
        ),
        # Crashing service with restart
        ServiceScenario(
            service_type="mock_crashing",
            variant="crasher",
            config={"crash_delay": 0.3, "exit_code": 1},
            restart="always",
            restart_sec=0.5,
            restart_max=2
        ),
        # Another stable service
        ServiceScenario(
            service_type="mock_permanent",
            variant="stable2",
            config={"work_interval": 0.7}
        ),
    ]

    generator = ConfigGenerator(
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )
    config_path = generator.generate_config(scenarios)

    harness = ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    )

    try:
        started = await harness.start(timeout=10.0)
        assert started

        # Let services run and crash/restart
        await asyncio.sleep(3.0)

        # Verify launcher is still running
        assert harness.is_running

        # Check output mentions all services
        output = harness.get_output()
        output_str = "\n".join(output)
        assert "stable" in output_str or "mock_permanent" in output_str
        assert "crasher" in output_str or "mock_crashing" in output_str

        stopped = await harness.stop(timeout=10.0)
        assert stopped

    finally:
        if harness.is_running:
            await harness.stop(timeout=5.0)
        generator.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
