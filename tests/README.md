# Test Infrastructure

Comprehensive testing infrastructure for ocabox-tcs with support for all launcher types and service configurations.

## Architecture

The test infrastructure follows a layered architecture designed for future extensibility:

### Layer 1: Core Fixtures (`fixtures/`)
- **`nats_fixtures.py`**: Isolated NATS server instances for testing
  - `NATSTestServer`: Manages NATS server lifecycle
  - `nats_server`: Pytest fixture providing server instance
  - `nats_client`: Pytest fixture providing connected client
  - `nats_url`: Pytest fixture providing server URL

### Layer 2: Test Helpers (`helpers/`)
- **`launcher_harness.py`**: Universal launcher abstraction
  - `LauncherHarness`: Base class for all launcher types
  - `ProcessHarness`: Process-based launcher testing with NATS status queries
  - `AsyncioHarness`: Asyncio-based launcher testing
  - `ServiceScenario`: Declarative service definitions

- **`config_generator.py`**: Configuration generation utilities
  - `ConfigGenerator`: Main config generator class
  - Helper functions for common scenarios:
    - `create_simple_config()`: Single service configs
    - `create_multi_service_config()`: Multi-service configs
    - `create_crash_test_config()`: Crash testing configs
    - `create_restart_limit_config()`: Restart limit testing

- **`event_collector.py`**: NATS event collection and verification (Phase 2)
  - `NATSEventCollector`: Collect and query NATS events
  - `CollectedEvent`: Event representation with filtering
  - Helper functions for stream-specific collection:
    - `collect_registry_events()`: Collect lifecycle events
    - `collect_status_events()`: Collect status updates
    - `collect_heartbeat_events()`: Collect heartbeats

- **`wait_helpers.py`**: Condition waiting utilities (Phase 2)
  - `wait_for_condition()`: Generic condition waiting
  - `wait_for_event()`: Wait for specific NATS event
  - `wait_for_event_sequence()`: Wait for event sequence
  - `wait_for_status()`: Wait for service status
  - `wait_for_launcher_ready()`: Wait for launcher startup
  - `wait_for_service_count()`: Wait for N services
  - `wait_for_no_events()`: Verify stability
  - `retry_until_success()`: Retry operations

- **`assertions.py`**: High-level assertion helpers (Phase 2)
  - `assert_service_started()`: Verify service startup
  - `assert_service_stopped()`: Verify graceful shutdown
  - `assert_service_crashed()`: Verify crash behavior
  - `assert_service_restarted()`: Verify restart cycles
  - `assert_restart_limit_reached()`: Verify restart limits
  - `assert_event_sequence()`: Verify event order
  - `assert_status_transition()`: Verify status changes
  - `assert_multiple_services_started()`: Verify batch startup
  - `assert_no_crashes()`: Verify stability
  - `assert_event_data()`: Verify event contents

### Layer 3: Mock Services (`services/`)
- **`mock_permanent.py`**: Basic permanent service with controllable behavior
  - Configurable work intervals
  - Optional startup/shutdown delays
  - Healthcheck status override
  - Work count limit (auto-stop after N iterations)

- **`mock_crashing.py`**: Service that deliberately crashes
  - Configurable crash delay and exit code
  - Multiple crash types (exit, exception, signal)
  - Crash on specific iteration
  - Used for testing restart policies

### Layer 4: Test Organization
```
tests/
├── unit/                    # Unit tests for individual components
│   ├── test_basic_lifecycle.py      # Basic service lifecycle
│   └── test_crash_scenarios.py      # Crash and restart testing
├── integration/             # Integration tests across components
├── launcher_specific/       # Launcher-specific behavior tests
│   ├── test_process_launcher.py
│   ├── test_asyncio_launcher.py
│   ├── test_systemd_launcher.py
│   └── test_container_launcher.py
├── service_types/          # Service type-specific tests
│   ├── test_permanent_services.py
│   ├── test_single_shot_services.py
│   └── test_cyclic_services.py
└── regression/             # Regression tests for bug fixes
    └── scenarios/          # Declarative scenario definitions
```

## Usage

### Running Tests

Run all tests:
```bash
poetry run pytest
```

Run specific test file:
```bash
poetry run pytest tests/unit/test_basic_lifecycle.py
```

Run with verbose output:
```bash
poetry run pytest -v
```

Run specific test:
```bash
poetry run pytest tests/unit/test_basic_lifecycle.py::test_simple_service_startup_shutdown -v
```

### Writing Tests

#### Basic Lifecycle Test
```python
import pytest
from tests.fixtures.nats_fixtures import nats_server
from tests.helpers.config_generator import create_simple_config
from tests.helpers.launcher_harness import ProcessHarness

@pytest.mark.asyncio
async def test_my_scenario(nats_server):
    # Generate config
    config_path = create_simple_config(
        service_type="mock_permanent",
        instance_context="test_basic",
        nats_host=nats_server.host,
        nats_port=nats_server.port
    )

    # Create harness
    async with ProcessHarness(
        config_path=config_path,
        nats_url=nats_server.url,
        capture_output=True
    ) as harness:
        # Launcher auto-starts in context
        await asyncio.sleep(1.0)

        # Verify behavior
        output = harness.get_output()
        assert "running" in " ".join(output).lower()

    # Launcher auto-stops on context exit
```

#### Multi-Service Test
```python
from tests.helpers.launcher_harness import ServiceScenario
from tests.helpers.config_generator import ConfigGenerator

scenarios = [
    ServiceScenario(
        service_type="mock_permanent",
        instance_context="service_1",
        config={"work_interval": 0.5}
    ),
    ServiceScenario(
        service_type="mock_crashing",
        instance_context="crasher",
        config={"crash_delay": 0.3},
        restart="always",
        restart_max=3
    )
]

generator = ConfigGenerator(
    nats_host=nats_server.host,
    nats_port=nats_server.port
)
config_path = generator.generate_config(scenarios)
```

#### Crash Testing
```python
from tests.helpers.config_generator import create_crash_test_config

config_path = create_crash_test_config(
    restart_policy="on-failure",
    crash_delay=0.5,
    exit_code=1,
    nats_host=nats_server.host,
    nats_port=nats_server.port,
    restart_max=3
)
```

#### Event Verification (Phase 2)
```python
from tests.helpers.event_collector import NATSEventCollector
from tests.helpers.assertions import assert_service_started, assert_service_crashed

# Collect events during test
async with NATSEventCollector(
    nats_client=nats_client,
    stream_name="svc_registry",
    subjects=["svc.registry.>"]
) as collector:
    # Run test scenario
    async with ProcessHarness(...) as harness:
        await asyncio.sleep(2.0)

    # Verify events
    await assert_service_started(collector, "mock_permanent:test")

    # Query events
    events = collector.get_events(event_type="start")
    assert len(events) > 0
```

#### Wait Helpers (Phase 2)
```python
from tests.helpers.wait_helpers import wait_for_event, wait_for_status

# Wait for specific event
found = await wait_for_event(
    collector,
    event_type="start",
    service_id="mock_permanent:test",
    timeout=5.0
)
assert found, "Service did not start"

# Wait for status transition
found = await wait_for_status(
    collector,
    service_id="mock_permanent:test",
    expected_status="ok",
    timeout=5.0
)
```

#### Assertion Helpers (Phase 2)
```python
from tests.helpers.assertions import (
    assert_service_started,
    assert_service_crashed,
    assert_event_sequence,
    assert_no_crashes
)

# High-level assertions
await assert_service_started(collector, "mock_permanent:test")
await assert_service_crashed(collector, "mock_crashing:test", expected_exit_code=1)
await assert_event_sequence(
    collector,
    "mock_crashing:test",
    expected_sequence=["start", "crashed", "restarting"]
)
await assert_no_crashes(collector, duration=5.0)
```

## Mock Service Configuration

### MockPermanentService
Environment variables (can be set via config dict):
- `work_interval`: Sleep interval in main loop (default: 0.5s)
- `startup_delay`: Delay during startup (default: 0.0s)
- `shutdown_delay`: Delay during shutdown (default: 0.0s)
- `healthcheck_status`: Override status (ok/degraded/warning/error)
- `work_count`: Stop after N iterations (0 = infinite)

Example config:
```yaml
services:
  - type: mock_permanent
    instance_context: test
    config:
      work_interval: 0.5
      startup_delay: 1.0
      work_count: 10
```

### MockCrashingService
Environment variables (can be set via config dict):
- `crash_delay`: Delay before crash (default: 0.5s)
- `exit_code`: Exit code for crash (default: 1)
- `crash_on_iteration`: Which iteration to crash (default: 1)
- `crash_type`: Type of crash (exit/exception/signal)
- `signal_number`: Signal for signal crash (default: SIGTERM)

Example config:
```yaml
services:
  - type: mock_crashing
    instance_context: crasher
    config:
      crash_delay: 0.3
      exit_code: 1
      crash_type: exit
    restart: always
    restart_max: 3
    restart_sec: 1.0
```

## Extending the Infrastructure

### Adding New Launcher Types

Create a new harness class:
```python
class SystemdHarness(LauncherHarness):
    async def start(self, timeout: float = 10.0) -> bool:
        # Implement systemd-specific start
        pass

    async def stop(self, timeout: float = 10.0) -> bool:
        # Implement systemd-specific stop
        pass
```

### Adding New Mock Services

Follow the pattern in `mock_permanent.py`:
1. Define config dataclass with `@config` decorator
2. Create service class with `@service` decorator
3. Implement `start_service()`, `stop_service()`, `run_service()`
4. Add controllable behavior via config parameters
5. Add standalone entry point for direct execution

## Best Practices

1. **Use Fixtures**: Always use `nats_server` fixture for isolated NATS
2. **Context Managers**: Use harness context managers for automatic cleanup
3. **Capture Output**: Enable output capture for debugging (`capture_output=True`)
4. **Adequate Timeouts**: Allow sufficient time for service lifecycle
5. **Cleanup**: Use try-finally or context managers to ensure cleanup
6. **Declarative Scenarios**: Use `ServiceScenario` for complex setups
7. **Temp Configs**: Use `temp=True` for automatic config cleanup

## Troubleshooting

### Tests Hang or Timeout
- Check NATS server is properly started (fixture should handle this)
- Increase test timeouts for slow operations
- Check launcher output: `harness.get_output()`

### Services Not Starting
- Verify config file generation: print `config_path.read_text()`
- Check NATS connectivity: use `nats_client` fixture for manual testing
- Review launcher output for errors

### Cleanup Issues
- Always use context managers or try-finally
- Ensure `harness.stop()` is called
- Use `generator.cleanup()` for temp config files

## Implementation Status

### ✅ Phase 1 (Completed):
- Core test infrastructure (fixtures, harnesses, generators)
- Mock service library (permanent, crashing)
- Example unit tests (lifecycle, crash scenarios)
- Basic documentation

### ✅ Phase 2 (Completed):
- Event collection and verification (`event_collector.py`)
- Wait helpers for robust timing (`wait_helpers.py`)
- High-level assertion helpers (`assertions.py`)
- NATS-based status queries in ProcessHarness
- Event verification test examples
- Enhanced documentation

**Next Phases** (see `doc/test-implementation-guideline.md`):
- Phase 3: Service type tests (permanent, single-shot, cyclic)
- Phase 4: Launcher-specific tests (process, asyncio, systemd, container)
- Phase 5: Regression test suite with scenarios

## References

- **Implementation Guideline**: `doc/test-implementation-guideline.md`
- **Architecture**: `doc/architecture.md`
- **NATS Schema**: `doc/nats.md`
