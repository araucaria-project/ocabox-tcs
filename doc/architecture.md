# Universal Service Framework Architecture

## Overview

This architecture provides a clean, non-redundant framework supporting the complete implementation matrix with proper separation of concerns between service processes and launcher processes.

**See also:** [Initialization Flow Diagrams](initialization-flow.md) for detailed visualization of all three execution scenarios.

## Core Design Principles

1. **Execution Independence**: One service works everywhere (manual/process/asyncio, with support for additional launchers)
2. **Minimal Service Implementation**: `HelloWorld` service requires minimum code
3. **Clear Separation**: Service process concerns vs launcher process concerns
4. **Unified Initialization**: `ProcessContext.initialize()` - one clear entry point
5. **Unified Monitoring**: Hierarchical status reporting via NATS
6. **Flexible Configuration**: Multiple config sources with clear precedence
7. **Decorator-Based**: Modern Python decorators for clean service registration

## Core Classes and Responsibilities

### BaseServiceConfig
- **Functionality**: Dataclass loaded from config file
- **Lifetime**: Created per service instance, provided to service class
- **Instance Count**: One per service instance
- **Dependencies**: Configuration files, validation schemas

### BaseService  
- **Functionality**: Service class to be overridden by concrete services
- **Lifetime**: Created and managed by ServiceController
- **Methods to Override**: `async def _start_service()`, `async def _stop_service()`
- **Design**: Minimal base class, no hidden functionality

### ServiceController
- **Functionality**: Controls single service in same process as service
- **Lifetime**: Created before service object, destroyed after service cleanup
- **Responsibilities**:
  - Creates service objects and calls lifecycle methods
  - Discovers concrete service and config classes  
  - Prepares configuration from multiple sources
  - Controls service health and status reporting (via MonitoredObject)
  - Maintains universal entry point for service
  - May exist and report failure even if service creation failed
- **Instance Count**: One per service
- **Process**: Same as service process

### ServiceRunner
- **Functionality**: Controls service lifetime from launcher process
- **Lifetime**: Exists for all services (running, stopped, periodic)
- **Responsibilities**:
  - Starting and finishing services
  - Specialized subclasses handle different execution methods
- **Instance Count**: One per service (may be different process than service)
- **Process**: Launcher process
- **Note**: For manually started services, ServiceRunner does not exist

### ServicesLauncher
- **Functionality**: Manages collection of ServiceRunners from config
- **Lifetime**: Persistent launcher process
- **Responsibilities**: Maintains launching system
- **Specialized Subclasses**: ProcessLauncher, AsyncioLauncher

### ProcessContext
- **Functionality**: Singleton context for all services in a process
- **Lifetime**: Process lifetime
- **Instance Count**: One per OS process (singleton)
- **Initialization**: `ProcessContext.initialize(config_file, args_config)` - dispatcher pattern
- **Responsibilities**:
  - Configuration management (file → NATS bootstrap)
  - NATS Messenger (singleton, shared)
  - Service registry (controllers in this process)
  - Two-phase config bootstrap (file/args → NATS)
- **Key Methods**:
  - `initialize()` - Main initialization dispatcher
  - `_init_config_manager()` - Phase 1: File + args config
  - `_init_messenger()` - Phase 2: NATS connection
  - `_add_nats_config_source()` - Phase 3: NATS config source

### MonitoredObject Hierarchy
```
MonitoredObject (base)
├── ReportingMonitoredObject (actively checks status periodically)
└── MessengerMonitoredObject (sends reports to NATS via serverish.Messenger)
```

- **MonitoredObject**: Base class with aggregation support, healthcheck callbacks, children management
- **ReportingMonitoredObject**: Root objects should be instances of this class
- **MessengerMonitoredObject**: Overrides `send` method for NATS communication
- **Aggregation**: Recursively aggregates children status
- **User Integration**: Manual registration of custom MonitoredObjects

## Service Discovery

### Decorator-Based Discovery (Primary)
```python
# File: hello_world.py
from ocabox_tcs.base_service import service, config, BasePermanentService, BaseServiceConfig

@config  # Optional - config classes can be omitted, service type derived from filename
@dataclass
class HelloWorldConfig(BaseServiceConfig):
    interval: int = 5
    message: str = "Hello World!"

@service  # Required - service type automatically derived from filename (hello_world.py → hello_world)
class HelloWorldService(BasePermanentService):
    async def start_service(self):
        while self.is_running:
            self.svc_logger.info(self.svc_config.message)
            await asyncio.sleep(self.svc_config.interval)
```

### Discovery Priority
1. **Decorator registry** (primary method)
2. **Module exports** (`service_class`, `config_class` variables if present)
3. **Convention-based** (class name patterns)
4. **Default fallback** (BaseServiceConfig if no config found)

## Monitoring Integration Example

```python
class MyService(BaseService):
    def __init__(self):
        super().__init__()
        self.controller.monitor.set_status(monitoring.Status.STARTUP)
        self.controller.monitor.add_healthcheck_cb(self.healthcheck)
        
        # Add subsystem monitoring
        self.subsystem = MySubsystem() 
        self.subsystem_monitor = MonitoredObject('my-subsystem')
        self.subsystem_monitor.add_status_cb(
            lambda: monitoring.Status.OK if self.subsystem.is_connected 
                   else monitoring.Status.ERROR
        )
        self.controller.monitor.add_submonitor(self.subsystem_monitor)
        self.controller.monitor.set_status(monitoring.Status.OK)
        
    def healthcheck(self):
        return monitoring.Status.OK if self.system_healthy else monitoring.Status.ERROR
```

## NATS Communication

### Who Talks via NATS?

1. **MonitoredObjects**:
   - Periodic status reports
   - Register on start for discovery  
   - Signal shutdown
   - RPC commands via serverish (health-check, stats)

2. **ServiceController**:
   - RPC controlling commands for service-specific functionality (planned)

3. **ServiceLauncher**:
   - Publishes "declared" services (including non-running ones)
   - Own status via MonitoredObject

### Discovery via JetStream
- Uses NATS JetStream messages as registration for discovery
- No central registry service
- Distributed discovery based on message history

## Execution Patterns

### Manual Execution
```bash
# ServiceController exists, no ServiceRunner
python -m ocabox_tcs.services.hello_world config.yaml hello_world dev
```

### Launcher-based Execution  
```yaml
# config/services.yaml
launchers:
  dev_launcher:
    type: process
    services:
      - module: ocabox_tcs.services.hello_world
        instance: dev
        runner_id: dev_launcher.hello_world_runner
```

### Runner ID Passing
- ServiceRunner constructs ID: `launcher_id.runner_id`
- ID passed to service as optional parameter
- ServiceController stores runner ID for reference

## Service Base Classes

```
BaseService (abstract)
├── BasePermanentService (basic permanent service)
├── BaseBlockingPermanentService (permanent with run_service loop)
└── BaseSingleShotService (one-time execution)
```

**Service Patterns**:
- **BasePermanentService**: Override `start_service()` and `stop_service()` with custom task management
- **BaseBlockingPermanentService**: Override `run_service()` for main loop + `on_start()`/`on_stop()` for setup/cleanup
- **BaseSingleShotService**: Override `execute()` for one-time tasks

## Implementation Matrix Coverage

| Service Type | Location | Execution | Configuration | ServiceController | ServiceRunner |
|-------------|----------|-----------|---------------|-------------------|---------------|
| Permanent | Local | Manual | File | ✓ | ✗ |
| Blocking | Local | Process | File | ✓ | ProcessRunner |
| SingleShot | Local | Asyncio | File | ✓ | AsyncioRunner |
| Any | External | Any | Any | ✓ | Any |
| Any | Any | Any | NATS | ✓ | Any |

## Directory Structure

```
src/ocabox_tcs/
├── base_service.py           # BaseService, BaseServiceConfig, decorators
├── launchers/                # ServiceLauncher + ServiceRunner classes
│   ├── base_launcher.py      # Abstract launcher classes
│   ├── process.py            # Process-based launcher & runner
│   └── asyncio.py            # Asyncio-based launcher & runner
├── management/               # Service process components
│   ├── service_controller.py # ServiceController
│   ├── process_context.py    # ProcessContext singleton (process-wide initialization)
│   └── configuration.py      # Configuration management
├── monitoring/               # Monitoring framework
│   ├── monitored_object.py      # MonitoredObject hierarchy
│   ├── monitored_object_nats.py # NATS-enabled monitoring
│   └── status.py                # Status enums and utilities
└── services/                 # Built-in services
    ├── hello_world.py        # Canonical template service
    ├── examples/             # Tutorial examples
    │   ├── 01_minimal.py     # Simplest service
    │   ├── 02_basic.py       # With configuration
    │   ├── 03_logging.py     # Logging best practices
    │   ├── 04_monitoring.py  # Monitoring & health checks
    │   └── README.md         # Getting Started guide
    └── ...
```

This architecture provides clean separation of concerns while maintaining simplicity for service implementers and supporting all execution scenarios.