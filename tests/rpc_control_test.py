"""RPC control tests for remote service management.

This test demonstrates best practices for testing RPC endpoints:
- Using pytest fixtures for configuration
- Async test functions with @pytest.mark.asyncio
- Parametrized tests for multiple commands
- Proper error handling and assertions
- Skipping tests when target service is unavailable

Educational example for proper pytest structure.

Run with:
    pytest tests/rpc_control_test.py -v
    pytest tests/rpc_control_test.py::test_rpc_state -v  # Run specific test

Skip if service unavailable:
    pytest tests/rpc_control_test.py -v -m "not manual"
"""

import os
import pytest
from serverish.messenger import Messenger, request
from serverish.base.exceptions import (
    MessengerRequestNoResponders,
    MessengerRequestNoResponse,
    MessengerRequestTimeout
)


# ============================================================================
# Fixtures - Reusable test setup
# ============================================================================

@pytest.fixture
def rpc_host():
    """Get RPC host from environment or use default.

    Set environment variable: export RPC_HOST=192.168.8.140
    """
    return os.environ.get('RPC_HOST', '192.168.8.140')


@pytest.fixture
def rpc_port():
    """Get RPC port from environment or use default.

    Set environment variable: export RPC_PORT=4222
    """
    return int(os.environ.get('RPC_PORT', '4222'))


@pytest.fixture
def rpc_service_subject():
    """Get RPC service subject from environment or use default.

    Set environment variable: export RPC_SERVICE=tic.rpc.dev.dome.follower
    """
    return os.environ.get('RPC_SERVICE', 'tic.rpc.dev.dome.follower')


@pytest.fixture
async def messenger(rpc_host, rpc_port):
    """Create Messenger context for RPC calls.

    This fixture automatically connects and disconnects from NATS.
    """
    async with Messenger().context(host=rpc_host, port=rpc_port) as m:
        yield m


# ============================================================================
# Tests - Marked as manual to skip by default
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.manual  # Skip by default - requires external service
async def test_rpc_state(messenger, rpc_service_subject):
    """Test RPC state query command.

    Verifies that we can query the service state via RPC.
    """
    try:
        dat, met = await request(subject=f'{rpc_service_subject}.state')

        # Assert we got a response
        assert dat is not None, "Expected data in response"

        # Assert response has expected structure
        assert 'status' in dat, "Response should contain 'status' field"

        # Log the response for debugging
        print(f"State response: {dat}")

    except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout) as e:
        pytest.skip(f"Service not available: {e}")


@pytest.mark.asyncio
@pytest.mark.manual  # Skip by default - requires external service
async def test_rpc_on_command(messenger, rpc_service_subject):
    """Test RPC 'on' command.

    Verifies that we can turn the service on via RPC.
    """
    try:
        dat, met = await request(subject=f'{rpc_service_subject}.on')

        # Assert we got a response
        assert dat is not None, "Expected data in response"

        # Assert command succeeded
        assert dat.get('status') == 'ok', f"Expected status='ok', got: {dat.get('status')}"

        print(f"On command response: {dat}")

    except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout) as e:
        pytest.skip(f"Service not available: {e}")


@pytest.mark.asyncio
@pytest.mark.manual  # Skip by default - requires external service
async def test_rpc_off_command(messenger, rpc_service_subject):
    """Test RPC 'off' command.

    Verifies that we can turn the service off via RPC.
    """
    try:
        dat, met = await request(subject=f'{rpc_service_subject}.off')

        # Assert we got a response
        assert dat is not None, "Expected data in response"

        # Assert command succeeded
        assert dat.get('status') == 'ok', f"Expected status='ok', got: {dat.get('status')}"

        print(f"Off command response: {dat}")

    except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout) as e:
        pytest.skip(f"Service not available: {e}")


# ============================================================================
# Parametrized test - Multiple commands in one test
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.manual  # Skip by default - requires external service
@pytest.mark.parametrize("command,expected_status", [
    ("state", None),  # State query may return any status
    ("on", "ok"),     # On command should return ok
    ("off", "ok"),    # Off command should return ok
])
async def test_rpc_commands_parametrized(messenger, rpc_service_subject, command, expected_status):
    """Parametrized test for multiple RPC commands.

    This runs the same test logic for multiple commands.
    Demonstrates pytest parametrization for cleaner test code.
    """
    try:
        dat, met = await request(subject=f'{rpc_service_subject}.{command}')

        # Assert we got a response
        assert dat is not None, f"Expected data in response for command '{command}'"

        # Check expected status if specified
        if expected_status:
            actual_status = dat.get('status')
            assert actual_status == expected_status, \
                f"Command '{command}' expected status='{expected_status}', got: '{actual_status}'"

        print(f"Command '{command}' response: {dat}")

    except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout) as e:
        pytest.skip(f"Service not available for command '{command}': {e}")


# ============================================================================
# Main - For manual execution only (NOT run during pytest collection)
# ============================================================================

if __name__ == '__main__':
    """Manual execution for quick testing.

    This block ONLY runs when the file is executed directly:
        python tests/rpc_control_test.py

    It does NOT run during pytest collection, preventing hangs.
    """
    print("Running manual RPC test...")
    print("For proper testing, use: pytest tests/rpc_control_test.py -v -m manual")

    # Run pytest programmatically with manual tests enabled
    pytest.main([__file__, "-v", "-m", "manual"])
