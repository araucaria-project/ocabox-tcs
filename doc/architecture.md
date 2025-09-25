# Universal Service Framework Architecture

## Overview

This architecture provides a clean, non-redundant framework supporting the complete implementation matrix with proper separation of concerns between service processes and launcher processes.

## Core Design Principles

1. **Execution Independence**: One service works everywhere (manual/process/asyncio/systemd/containers)
2. **Minimal Service Implementation**: `HelloWorld` service requires minimum code
3. **Clear Separation**: Service process concerns vs launcher process concerns
4. **Unified Monitoring**: Hierarchical status reporting via NATS
5. **Flexible Configuration**: Multiple config sources with clear precedence
6. **Decorator-Based**: Modern Python decorators for clean service registration

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
- **Note**: For manually started services, ServiceRunner does not exist initially. Future extension could add DefaultServiceRunner if needed.

### ServicesLauncher
- **Functionality**: Manages collection of ServiceRunners from config
- **Lifetime**: Persistent launcher process
- **Responsibilities**: Maintains launching system
- **Specialized Subclasses**: ProcessLauncher, AsyncioLauncher, SystemdLauncher

### ServicesProcess  
- **Functionality**: Singleton containing common functionality for all ServiceControllers in process
- **Lifetime**: Process lifetime
- **Instance Count**: One per service process
- **Responsibilities**: Shared resources, NATS connections, process-wide coordination

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
            self.logger.info(self.config.message)
            await asyncio.sleep(self.config.interval)
```

### Discovery Priority
1. **Decorator registry** (highest priority)
2. **Legacy exports** (`service_class`, `config_class` variables)  
3. **Convention-based** (class name patterns)
4. **Default fallback** (BaseServiceConfig if no config found)

### Legacy Support
Existing services with `service_class` and `config_class` exports continue working unchanged.

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
   - Future: RPC controlling commands for service-specific functionality

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
- ServiceController stores runner ID for future use
- **Note**: No DefaultServiceRunner implementation yet - reserved for future extension

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
| Any | Local | Systemd | File | ✓ | SystemdRunner |
| Any | External | Any | Any | ✓ | Any |
| Any | Any | Any | NATS | ✓ | Any |

## Directory Structure

```
src/ocabox_tcs/
├── base_service.py           # BaseService, BaseServiceConfig, decorators
├── launchers/                # ServiceLauncher + ServiceRunner classes  
│   ├── base_launcher.py      # Abstract launcher classes
│   ├── process_launcher.py   # Process-based launcher & runner
│   ├── asyncio_launcher.py   # Asyncio-based launcher & runner
│   └── systemd_launcher.py   # Systemd-based launcher & runner
├── management/               # Service process components
│   ├── service_controller.py # ServiceController
│   ├── services_process.py   # ServicesProcess singleton
│   └── configuration.py      # Configuration management
├── monitoring/               # Monitoring framework
│   ├── monitored_object.py   # MonitoredObject hierarchy
│   └── status.py            # Status enums and utilities
└── services/                 # Built-in services
    ├── hello_world.py        # Minimal example
    ├── dumb_permanent.py     # Current example
    └── ...
```

## Migration Path

1. **Phase 1**: Implement MonitoredObject hierarchy and ServiceController
2. **Phase 2**: Add decorator-based discovery with legacy support
3. **Phase 3**: Create launcher/runner separation (keep existing dev_launcher compatibility)
4. **Phase 4**: Add NATS monitoring integration
5. **Phase 5**: Migrate existing services to new pattern
6. **Phase 6**: Add additional launcher types (systemd, containers)

This architecture provides clean separation of concerns while maintaining simplicity for service implementers and supporting all execution scenarios.