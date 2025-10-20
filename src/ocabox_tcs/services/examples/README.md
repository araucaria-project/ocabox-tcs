# TCS Service Examples - Getting Started

This directory contains progressive examples showing how to build TCS services, from the simplest possible implementation to more advanced patterns.

## Quick Start

### Prerequisites

```bash
# Install dependencies
poetry install

# Optional: Start NATS server for full functionality
# (Examples work without NATS but won't have distributed monitoring)
docker run -p 4222:4222 nats:latest
```

### Running Examples

Each example can be run in three ways:

1. **Standalone** - Single service in its own process
2. **Asyncio Launcher** - All services in one process (shared resources)
3. **Process Launcher** - Each service in separate subprocess

## Example Progression

### 1. Minimal Service (`01_minimal.py`)

The absolute simplest TCS service - perfect starting point.

**What it demonstrates:**
- Bare minimum service implementation (< 30 lines)
- `@service` decorator for registration
- Basic async service loop
- Service type automatically derived from filename

**Run it:**
```bash
# Standalone
python src/ocabox_tcs/services/examples/01_minimal.py config/examples.yaml minimal

# With asyncio launcher (all examples together)
poetry run tcs_asyncio --config config/examples.yaml

# With process launcher (separate processes)
poetry run tcs_process --config config/examples.yaml
```

**Expected output:**
```
[INFO ] svc.01_minimal:minimal: Service running...
[INFO ] svc.01_minimal:minimal: Service running...
...
```

---

### 2. Basic Service (`02_basic.py`)

Adds configuration support to your service.

**What it demonstrates:**
- Custom configuration using dataclasses
- `@config` decorator for config registration
- Reading config values in service code
- YAML configuration binding

**Configuration** (`config/examples.yaml`):
```yaml
services:
  - type: 02_basic
    instance_context: basic
    interval: 2.0
    message: "Basic service running"
```

**Run it:**
```bash
python src/ocabox_tcs/services/examples/02_basic.py config/examples.yaml basic
```

**Expected output:**
```
[INFO ] svc.02_basic:basic: Basic service running (every 2.0s)
[INFO ] svc.02_basic:basic: Basic service running (every 2.0s)
...
```

---

### 3. Logging Service (`03_logging.py`)

Demonstrates logging best practices.

**What it demonstrates:**
- Different log levels (DEBUG, INFO, WARNING, ERROR)
- When to use each log level
- Error handling in service loops
- Lifecycle hooks (`on_start`, `on_stop`)

**Run it:**
```bash
python src/ocabox_tcs/services/examples/03_logging.py config/examples.yaml logging
```

**Expected output:**
```
[INFO ] svc.03_logging:logging: Logging service starting up
[INFO ] svc.03_logging:logging: Processing cycle 1
[INFO ] svc.03_logging:logging: Processing cycle 2
[WARN ] svc.03_logging:logging: Cycle 5 - entering maintenance mode
[ERROR] svc.03_logging:logging: Caught error in cycle 10: Simulated error
...
```

---

### 4. Monitoring Service (`04_monitoring.py`)

Shows the monitoring and health check framework.

**What it demonstrates:**
- Status reporting (OK, DEGRADED, ERROR, FAILED)
- Health check callbacks
- Error tracking and recovery
- Automatic shutdown on critical failures
- Integration with monitoring system

**Run it:**
```bash
python src/ocabox_tcs/services/examples/04_monitoring.py config/examples.yaml monitoring
```

**Expected output:**
```
[INFO ] svc.04_monitoring:monitoring: Monitoring service ready
[INFO ] svc.04_monitoring:monitoring: Cycle 1 completed
[INFO ] svc.04_monitoring:monitoring: Cycle 2 completed
[ERROR] svc.04_monitoring:monitoring: Error in cycle 10: Simulated error
[INFO ] svc.04_monitoring:monitoring: Cycle 11 completed
...
```

---

## Running with Launchers

### Asyncio Launcher (Single Process)

All services run in the same process, sharing resources like NATS connections.

**Benefits:**
- Fast startup
- Shared NATS messenger
- Lower memory usage
- Good for development

**Usage:**
```bash
poetry run tcs_asyncio --config config/examples.yaml
```

**Expected output:**
```
2025-10-01 15:30:45.123 [INFO ] [ctx           ] ProcessContext initialized
2025-10-01 15:30:45.234 [INFO ] [launch.asyncio-launcher] Using ProcessContext
2025-10-01 15:30:45.345 [INFO ] [run.01_minimal-minimal ] Service 01_minimal-minimal started in-process
2025-10-01 15:30:45.456 [INFO ] [run.02_basic-basic     ] Service 02_basic-basic started in-process
2025-10-01 15:30:45.567 [INFO ] [run.03_logging-logging ] Service 03_logging-logging started in-process
2025-10-01 15:30:45.678 [INFO ] [run.04_monitoring-monitoring] Service 04_monitoring-monitoring started in-process
...
```

---

### Process Launcher (Multiple Processes)

Each service runs in its own subprocess.

**Benefits:**
- Process isolation
- Independent resource limits
- Can restart services individually
- Production-ready

**Usage:**
```bash
poetry run tcs_process --config config/examples.yaml
```

**Expected output:**
```
2025-10-01 15:30:45.123 [INFO ] [ctx           ] ProcessContext initialized
2025-10-01 15:30:45.234 [INFO ] [launch.process-launcher] Using ProcessContext
2025-10-01 15:30:45.345 [INFO ] [run.01_minimal-minimal ] Starting service: poetry run python -m ocabox_tcs.services.examples.01_minimal
2025-10-01 15:30:46.456 [INFO ] [run.01_minimal-minimal ] Service 01_minimal-minimal started (PID: 12345)
...
```

---

## Understanding the Output

### Logger Names

The compact logger names follow this pattern:

| Prefix | Meaning | Example |
|--------|---------|---------|
| `ctx` | ProcessContext | `ctx` |
| `cfg` | Configuration | `cfg` |
| `ctrl` | ServiceController | `ctrl.hello_world:dev` |
| `svc` | Service instance | `svc.hello_world:dev` |
| `launch` | Launcher | `launch.asyncio-launcher` |
| `run` | ServiceRunner | `run.hello_world-dev` |
| `mon` | Monitor | `mon.hello_world:dev` |

### Log Formats

**Launcher logs** (detailed):
```
2025-10-01 15:30:45.123 [INFO ] [ctx           ] ProcessContext initialized
       ^timestamp    ^msecs  ^level  ^logger name    ^message
```

**Service logs** (basic):
```
[INFO ] svc.hello:dev: Hello World!
 ^level ^logger      ^message
```

Services use basic format because the process launcher wraps their output with prefixes.

---

## Next Steps

After working through these examples:

1. **Read the Architecture** - [doc/architecture.md](../../../../doc/architecture.md)
2. **Development Guide** - [doc/development-guide.md](../../../../doc/development-guide.md)
3. **Try the Real Services** - See `dumb_permanent.py` and `dumb_complex.py`
4. **Build Your Own** - Create a new service in `src/ocabox_tcs/services/`

---

## Common Patterns

### Creating a New Service

1. **Choose a base class:**
   - `BaseBlockingPermanentService` - Continuous loop (most common)
   - `BasePermanentService` - Manual start/stop control
   - `BaseSingleShotService` - One-time execution

2. **Create service file:**
   ```python
   # src/ocabox_tcs/services/my_service.py
   from ocabox_tcs.base_service import BaseBlockingPermanentService, service

   @service
   class MyService(BaseBlockingPermanentService):
       async def run_service(self):
           while self.is_running:
               # Your logic here
               await asyncio.sleep(1)

   if __name__ == '__main__':
       MyService.main()
   ```

3. **Add configuration** (optional):
   ```python
   from dataclasses import dataclass
   from ocabox_tcs.base_service import BaseServiceConfig, config

   @config
   @dataclass
   class MyServiceConfig(BaseServiceConfig):
       my_setting: str = "default"
   ```

4. **Add to config file:**
   ```yaml
   services:
     - type: my_service
       instance_context: main
       my_setting: "custom value"
   ```

5. **Run it:**
   ```bash
   python src/ocabox_tcs/services/my_service.py config/services.yaml main
   ```

---

## Troubleshooting

### Service not found

**Error:** `Could not find service class in ocabox_tcs.services.my_service`

**Fix:** Make sure you used the `@service` decorator on your service class.

### Config fields not recognized

**Error:** Config values not being applied

**Fix:**
- Use the `@config` decorator on your config class
- Ensure config class inherits from `BaseServiceConfig`
- Check field names match between YAML and dataclass

### NATS connection timeout

**Warning:** `Failed to initialize NATS messenger`

**Fix:**
- Set `required: false` in NATS config for optional NATS
- Or start NATS server: `docker run -p 4222:4222 nats:latest`

---

## Questions or Issues?

- **Documentation:** [doc/](../../../../doc/)
- **GitHub Issues:** [Report issues](https://github.com/araucaria-project/ocabox-tcs/issues)
- **Architecture:** [doc/architecture.md](../../../../doc/architecture.md)
