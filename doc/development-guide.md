# Service Development Guide

## Choosing the Right Base Class

### Quick Decision Tree

**Does your service run once and finish?**
→ Use `BaseSingleShotService`

**Does your service run continuously?**
→ **Does it have a simple main loop?**
  → Use `BaseBlockingPermanentService` 
→ **Does it need complex task management?**
  → Use `BasePermanentService`

## Base Class Details

### 🔄 BaseSingleShotService
**Use when**: Service runs once and terminates (imports, migrations, one-time tasks)

**Override**: `async def execute(self)`

**Example** (File: `data_importer.py`):
```python
@service
class DataImporterService(BaseSingleShotService):
    async def execute(self):
        # Import data once
        await self.import_data_from_file()
        self.svc_logger.info("Import completed")
```

---

### 🔁 BaseBlockingPermanentService ⭐ **RECOMMENDED for most services**
**Use when**: Service has a main loop and runs continuously

**Override**: 
- `async def run_service(self)` - main loop
- `async def on_start(self)` - setup (optional)
- `async def on_stop(self)` - cleanup (optional)

**Benefits**: 
- ✅ Automatic task management
- ✅ Clean cancellation handling
- ✅ Error handling
- ✅ No manual `asyncio.create_task()`

**Example** (File: `telescope_monitor.py`):
```python
@service
class TelescopeMonitorService(BaseBlockingPermanentService):
    async def on_start(self):
        # Setup connections, initialize hardware
        await self.connect_to_telescope()

    async def run_service(self):
        while self.is_running:
            # Main monitoring loop
            status = await self.check_telescope_status()
            await self.process_status(status)
            await asyncio.sleep(1)

    async def on_stop(self):
        # Cleanup
        await self.disconnect_from_telescope()
```

---

### ⚙️ BasePermanentService
**Use when**: Service needs complex task management or multiple concurrent tasks

**Override**: 
- `async def start_service()` - custom startup
- `async def stop_service()` - custom cleanup

**When to choose**:
- Multiple concurrent tasks
- Complex task coordination
- Custom task lifecycle needs
- Advanced patterns

**Example** (File: `complex_processor.py`):
```python
@service
class ComplexProcessorService(BasePermanentService):
    async def start_service(self):
        # Start multiple concurrent tasks
        self.task1 = asyncio.create_task(self.process_queue_a())
        self.task2 = asyncio.create_task(self.process_queue_b())
        self.monitor_task = asyncio.create_task(self.monitor_health())

    async def stop_service(self):
        # Custom cleanup for multiple tasks
        for task in [self.task1, self.task2, self.monitor_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
```

## Configuration Guide

### Optional Config Classes
Config classes are **optional**. If you don't need custom configuration:

**Example** (File: `simple_service.py`):
```python
@service  # No @config needed
class SimpleService(BaseBlockingPermanentService):
    async def run_service(self):
        # Uses BaseServiceConfig automatically
        self.svc_logger.info(f"Service type: {self.svc_config.type}")
```

### Custom Config Classes
Add custom configuration when needed:

**Example** (File: `advanced_service.py`):
```python
@config
@dataclass
class AdvancedConfig(BaseServiceConfig):
    timeout: int = 30
    max_retries: int = 3
    api_url: str = "https://api.example.com"

@service
class AdvancedService(BaseBlockingPermanentService):
    async def run_service(self):
        # Access custom config
        response = await self.call_api(self.svc_config.api_url)
```

## Best Practices

### ✅ Do
- Use `BaseBlockingPermanentService` for most services with loops
- Use absolute imports: `from ocabox_tcs.base_service import service`
- Keep `run_service()` focused on the main loop
- Use `on_start()`/`on_stop()` for setup/cleanup
- Check `self.is_running` in loops
- Handle `asyncio.CancelledError` gracefully

### ❌ Don't
- Manually create tasks in `BaseBlockingPermanentService` (framework handles it)
- Use relative imports (`from ..base_service`)
- Block in `on_start()` or `on_stop()` 
- Ignore cancellation exceptions
- Put setup/cleanup logic in `run_service()`

## Migration from Old Patterns

### Old Pattern (manual task management):
```python
class OldService(BasePermanentService):
    async def start_service(self):
        self._task = asyncio.create_task(self._main_loop())
    
    async def stop_service(self):
        if self._task:
            self._task.cancel()
            await self._task
    
    async def _main_loop(self):
        while self.is_running:
            # work
            await asyncio.sleep(1)
```

### New Pattern (framework-managed):
```python
class NewService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            # same work
            await asyncio.sleep(1)
```

**Result**: 50% less code, automatic error handling, cleaner cancellation! 🎉

## External Services

Services can be loaded from external Python packages, not just the built-in `ocabox_tcs.services` directory.

### When to Use External Services

- **Distributed development**: Different teams own different service packages
- **Reusability**: Services can be published as separate Python packages
- **Modularity**: Large projects can split services into multiple packages
- **Third-party integrations**: Import services from external libraries

### Creating an External Service

External services use the same `@service` decorator as built-in services:

**Example** (File: `my_project/telescope_control.py`):
```python
from ocabox_tcs.base_service import service, BaseBlockingPermanentService

@service
class TelescopeControlService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            await self.control_telescope()
            await asyncio.sleep(1)
```

### Registering External Services in Configuration

Use the `module` field to specify the full Python module path:

**Example** (File: `config/services.yaml`):
```yaml
services:
  # Built-in service (no module field needed)
  - type: hello_world
    instance_context: main

  # External service (from external package)
  - type: telescope_control
    instance_context: jk15
    module: my_project.telescope_control

  # Another external service example
  - type: data_importer
    instance_context: main
    module: external_package.importers.data_importer
```

### How It Works

1. **Configuration Loading**: Launchers read the `services` array from config
2. **Module Resolution**:
   - If `module` field is provided: Uses that module path
   - If `module` is missing: Defaults to `ocabox_tcs.services.{type}`
3. **Service Discovery**: The `@service` decorator registers the class globally
4. **Instantiation**: Launchers create instances using the registered class

### Service Registration Behavior

The `@service` decorator automatically derives service type names:

- **Built-in services**: From filename stem
  - `hello_world.py` → service type `"hello_world"`
  - `examples/01_minimal.py` → service type `"examples.01_minimal"`

- **External services**: From filename stem (no path prefix needed)
  - `my_project/telescope_control.py` → service type `"telescope_control"`
  - (The `module` field in config specifies the full package path)

**Key Point**: The `type` field in config must match the filename stem, not the full module path. The `module` field specifies where to find it.

### Testing External Services

Create tests to verify external services are discoverable:

```python
import importlib
from ocabox_tcs.base_service import get_service_class

# Import the module (triggers decorator registration)
importlib.import_module("my_project.telescope_control")

# Verify service is registered
service_class = get_service_class("telescope_control")
assert service_class is not None
```

### Example: External Service Package Structure

```
my_telescope_project/
├── my_project/
│   ├── __init__.py
│   ├── telescope_control.py      # External service
│   ├── data_importer.py          # Another service
│   └── config/
│       └── equipment.yaml
├── tests/
│   └── test_services.py
└── pyproject.toml                # Package metadata
```

### Using External Services with Launchers

Both `ProcessLauncher` and `AsyncioLauncher` support external services automatically:

```bash
# Process launcher with external services
poetry run tcs_process --config config/services.yaml

# Asyncio launcher with external services
poetry run tcs_asyncio --config config/services.yaml
```

The launchers resolve module paths and import services transparently.

## Error Handling

The framework automatically manages status transitions when exceptions occur. Understanding how errors map to status values helps you handle failures gracefully.

### Status Values for Errors

**Two distinct error states:**

- **`FAILED`**: Fatal error during startup or initialization
  - Service cannot start or is completely unusable
  - Set during: initialization, service creation, startup
  - Example: Port already in use, configuration invalid

- **`ERROR`**: Runtime error during execution
  - Service is operational but encountered a problem
  - Set during: service execution, shutdown
  - Example: Database connection lost, API timeout
  - Recovery possible via healthcheck or manual correction

**Other relevant statuses:**

- **`DEGRADED`**: Service operational with non-critical issues
- **`WARNING`**: Service has warnings but continues normally
- **`OK`**: Service running normally
- **`BUSY`/`IDLE`**: Service tracking task execution (with task tracking enabled)

### Exception Handling Flow

**Where exceptions are caught and converted to status:**

#### 1. **Initialization Errors** → `FAILED`
```python
# In ServiceController.initialize() (lines 81-86)
try:
    # Load class, setup config, initialize monitoring
    ...
except Exception as e:
    monitor.set_status(Status.FAILED, f"Initialization failed: {e}")
    return False
```

**Examples:**
- Service module not found
- Configuration file invalid
- Monitoring setup failed

#### 2. **Startup Errors** → `FAILED`
```python
# In ServiceController.start_service() (lines 100-120)
try:
    monitor.set_status(Status.STARTUP, "Starting service")
    await self._create_service()
    await self._service._internal_start()
    monitor.set_status(Status.OK, "Service running")
    return True
except Exception as e:
    monitor.set_status(Status.FAILED, f"Startup failed: {e}")
    return False
```

**Examples:**
- Required resource unavailable (database, API)
- Incompatible Python version
- Missing dependencies

#### 3. **Runtime Errors** → `ERROR`
```python
# In BaseBlockingPermanentService._run_wrapper() (lines 391-401)
try:
    await self.run_service()
except asyncio.CancelledError:
    raise  # Expected when service stops
except Exception as e:
    self.svc_logger.error(f"Error in run_service: {e}")
    monitor.set_status(Status.ERROR, f"Runtime error: {e}")
    raise  # Re-raise for controller awareness
```

**Examples:**
- Network error during operation
- Timeout waiting for resource
- Data processing error

#### 4. **Shutdown Errors** → `ERROR`
```python
# In ServiceController.stop_service() (lines 127-142)
try:
    monitor.set_status(Status.SHUTDOWN, "Stopping service")
    await self._service._internal_stop()
    return True
except Exception as e:
    monitor.set_status(Status.ERROR, f"Shutdown failed: {e}")
    return False
```

**Examples:**
- Cleanup failed (file flush error, connection close failed)
- Graceful shutdown timeout

### Two Ways to Handle Errors

#### **Option 1: Automatic via Healthcheck (Recommended)**

Define a healthcheck callback that returns status based on internal state:

```python
@service
class MyService(BaseBlockingPermanentService):
    def __init__(self):
        super().__init__()
        self.error_count = 0
        self.max_errors = 3

    async def run_service(self):
        while self.is_running:
            try:
                await self.do_work()
                self.error_count = 0  # Reset on success
            except Exception as e:
                self.error_count += 1
                self.svc_logger.error(f"Error: {e}")
                await asyncio.sleep(1)  # Back off before retry

    def healthcheck(self) -> Status | None:
        """Called periodically (default every 30s) to update status."""
        if self.error_count >= self.max_errors:
            return Status.FAILED  # Too many errors, give up
        elif self.error_count > 0:
            return Status.DEGRADED  # Some errors, but still trying
        return None  # Healthy, keep current status
```

**Advantages:**
- ✅ Automatic status updates via monitoring loop
- ✅ Responsive to internal state changes
- ✅ No manual status calls needed

#### **Option 2: Manual Status Override**

Call `monitor.set_status()` directly for immediate updates:

```python
@service
class MyService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            try:
                result = await self.critical_operation()
                if not result:
                    self.monitor.set_status(Status.ERROR, "Operation failed")
                else:
                    self.monitor.set_status(Status.OK, "Operation successful")
            except Exception as e:
                self.monitor.set_status(Status.ERROR, str(e))
```

**Advantages:**
- ✅ Immediate status change (not waiting for healthcheck loop)
- ✅ Fine-grained control over status messages

**Use when:**
- You need instant status change before next healthcheck cycle
- Status changes based on external events

### Recovering from Errors

#### **Healthcheck-based Recovery**

When the underlying issue is resolved, healthcheck automatically updates status:

```python
def healthcheck(self) -> Status | None:
    # Check if issue is resolved
    if self.connection.is_active():
        # Recovered!
        return Status.OK
    else:
        return Status.ERROR
```

#### **Manual Recovery with `cancel_error_status()`**

Use this to manually recover from ERROR/FAILED/DEGRADED status:

```python
@service
class MyService(BaseBlockingPermanentService):
    async def on_start(self):
        # Register recovery command handler
        self.add_healthcheck_cb(self.handle_recovery)

    async def handle_recovery(self) -> Status | None:
        if self.recovery_requested:
            self.recovery_requested = False
            self.monitor.cancel_error_status()  # Revert to OK/IDLE/BUSY
            return Status.OK
        return None
```

**What `cancel_error_status()` does:**
- If in ERROR/FAILED/DEGRADED: Reverts to OK (or IDLE/BUSY if task tracking enabled)
- Sets message to "Error resolved"
- Useful for manual recovery procedures

### Practical Example: Resilient Service

```python
@service
class ResilientService(BaseBlockingPermanentService):
    def __init__(self):
        super().__init__()
        self.connection = None
        self.errors_since_last_recovery = 0
        self.max_errors_before_critical = 5

    async def on_start(self):
        """Setup called before main loop."""
        try:
            self.connection = await self.connect_to_database()
        except Exception as e:
            self.svc_logger.error(f"Failed to connect: {e}")
            raise  # Will set status to FAILED

    async def run_service(self):
        """Main loop with error recovery."""
        while self.is_running:
            try:
                data = await self.fetch_data()
                await self.process_data(data)
                self.errors_since_last_recovery = 0  # Reset counter

            except ConnectionError as e:
                self.errors_since_last_recovery += 1
                self.svc_logger.warning(f"Connection error #{self.errors_since_last_recovery}: {e}")
                # Try to reconnect
                try:
                    self.connection = await self.connect_to_database()
                except Exception:
                    self.svc_logger.error("Reconnect failed, backing off...")
                    await asyncio.sleep(min(2 ** self.errors_since_last_recovery, 60))

            except Exception as e:
                self.svc_logger.error(f"Unexpected error: {e}")
                raise  # Will set status to ERROR, but service still running

            await asyncio.sleep(1)

    def healthcheck(self) -> Status | None:
        """Periodic health check (default every 30s)."""
        if self.errors_since_last_recovery >= self.max_errors_before_critical:
            return Status.FAILED  # Too many errors

        if self.errors_since_last_recovery > 0:
            return Status.DEGRADED  # Some errors but recovering

        if not self.connection or not self.connection.is_active():
            return Status.ERROR  # Connection lost

        return None  # Healthy
```

**Flow with this example:**
1. Normal operation: Status = OK
2. Connection error: Error logged, status = DEGRADED (via healthcheck)
3. Reconnect successful: Status = OK
4. Multiple reconnect failures: Status = FAILED (too many errors)
5. Admin fixes database: healthcheck detects it, status = OK again

### Status Aggregation

When a launcher manages multiple services, overall status reflects the worst service:

```
Services: [OK, OK, DEGRADED, ERROR]
Launcher Status: ERROR (worst case wins)

Services: [OK, FAILED]
Launcher Status: FAILED (always reported to NATS)
```

See `aggregate_status()` in `monitoring/status.py` for details.

### Viewing Errors in tcsctl

See service errors in the CLI:

```bash
# List running services only (default, hides stopped services)
tcsctl

# List all services including stopped ones
tcsctl --all

# Detailed multi-line view with all metadata
tcsctl --detailed

# Verbose mode shows collection statistics
tcsctl --verbose

# Filter to specific service
tcsctl hello_world

# Show legend explaining status symbols
tcsctl --legend
```

**Error Display:**
- Error states shown in **red** with × symbol:
  - `ERROR` - Service operational but with runtime errors
  - `FAILED` - Service startup failed, completely unusable
- Dead heartbeat shown as **⚠** in red (zombie process detection)
- Degraded/warning states shown in **yellow** with ◐ symbol
- Healthy services shown in **green** with ● symbol

### File Locations

| Aspect | File Location |
|--------|---------------|
| Exception handling in controller | `src/ocabox_tcs/management/service_controller.py` (lines 81-142) |
| Runtime error handling | `src/ocabox_tcs/base_service.py` (lines 391-401) |
| Status definitions | `src/ocabox_tcs/monitoring/status.py` |
| Healthcheck system | `src/ocabox_tcs/monitoring/monitored_object.py` (lines 225-247) |
| `cancel_error_status()` | `src/ocabox_tcs/monitoring/monitored_object.py` (lines 127-135) |
| Example service | `src/ocabox_tcs/services/examples/04_monitoring.py` |
| Tests | `tests/test_monitoring.py` |