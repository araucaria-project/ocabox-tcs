# Services Architecture

This project creates a framework for universal Python services in the telescope control system.

## Service Structure

### Package Structure
Each service is implemented as a Python package.

### Service Class
Services implement a service class that inherits from `ocabox_tcs.base_service.BaseService`, or from another class that inherits from `BaseService`. The project will have several base service classes with different functionalities (e.g., `BaseOCABoxService` contains OCABox API initialization).

### Configuration Class
Services implement a configuration class that inherits from `ocabox_tcs.base_service.BaseServiceConfig`. The configuration class contains all service configuration parameters. The system must ensure configuration retrieval and creation of configuration objects for services.

### Exported Elements
Services must export `service_class` and `config_class` variables from the package, which point to the service class and configuration class respectively.

**Note**: We should consider if this is the most minimalistic and elegant approach. Perhaps this could be solved differently to minimize the service creator's responsibilities while maintaining clean design.

### Required Methods
Currently, the following methods need to be overridden in the service class:
* `async def _start_service(self)`
* `async def _stop_service(self)`

Note that `start_service` currently blocks for permanent services (may be changed).

**TODO**: Review and properly organize service object lifecycle handling and inherited method structure to make service implementation flexible, convenient, and easy regardless of use case:
* Permanent service - user wants to block in some overridden method
* Permanent service - user wants to launch an asyncio task in the overridden method (same or another) without blocking
* Temporary service - user wants to execute a task and terminate

### Service Identification
To avoid multiplying entities, services are identified throughout the distributed system by package name and `instance_context` - the service instance name.

**Question**: Should we change something here or adopt a different convention?

## Permanent and Temporary/Periodic Services

Services can be permanent (e.g., plan_runner) or periodic/temporary (e.g., data importer).

- **Periodic services** can be launched on demand and terminate after completing their task
- **Permanent services** run continuously and can handle multiple tasks
- Launchers must support one-time or periodic service execution

## Service Locations

Currently, services are located in `ocabox_tcs.services`. Eventually, "standard" services should remain there, but the entire project must be built to allow services in other locations, projects, and repositories. In such cases, the project is imported as a dependency.

Launchers must be able to start services from different locations. This likely affects configuration management.

## Service Configuration

Currently, service configuration is in `config/services.yaml`. Launchers search for this file in a somewhat inelegant way. This needs to be solved properly.

Services expect to receive a path to such a file and search for "themselves" at the base level to retrieve their configuration.

**Considerations**:
- Different launchers have different needs
- Launcher configuration (which services to run and when) should also be in a configuration file
- Support for:
  - Files passed as launcher arguments
  - Dynamic configuration from NATS
  - Beyond the default `config/services.yaml`

## Service Execution

User services are independent of the execution method. A service written once must be executable by any method.

### Execution Methods

#### Manual Execution
Each service should have a `__main__` section and be executable as a file. The main function delegates startup to the service class's `app()` method.

#### Process-based
There is a runner that launches services as processes, currently called `ocabox_tcs.dev_launcher` (should probably be `ocabox_tcs.launchers.proc_launcher`).

#### Asyncio-based
**TODO**: Implement a launcher that runs services within a single asyncio process. Consider mixed execution (multiple services per process) and create a universal proc/asyncio launcher.

#### Systemd-based
**TODO**: Implement a launcher that uses systemd for service execution. Service templates and other systemd mechanisms should be used maximally. Basic templates already exist.

#### Docker/Kubernetes
In the future, consider running services in containers. Service creation should remain independent of execution method and relatively simple.

## Service Management and NATS

The launcher is partially responsible for service lifecycle management, though it uses system methods (e.g., systemd) where possible. Generally, it should maintain responsibility for service startup and shutdown. In this sense, the launcher is a Service Manager and knows the instances it has launched.

Services must universally cooperate with NATS (through `serverish.Messenger`). Services to be exposed on NATS, system-wide:

### Manager Responsibilities
- Service start, stop, restart
- Service configuration read from NATS (single or shared across multiple launchers)
- Service list (including defined but not running services)
- Service status (running, stopped, error, etc.)

### Service Responsibilities (base implementation)
- Service details (including host and launcher)
- Regular heartbeat from services
- Service health checks (with possibility to override in service class)

### Important Considerations
- Manually started services have no manager and should create their own
- Services should know they need a manager (perhaps launcher-started services should receive manager information as an argument?)
- This is one reason why the base launcher shouldn't be identical to the manager - it probably needs to create one too
- We don't want orphaned services without managers
- We don't want multiple managers handling the same service
- Launcher and/or manager may themselves be services in some scenarios

**Key**: Proper separation of responsibilities. The manager should actively monitor services to detect when systemd or other events kill a service.

## Shared Resources

If services are in the same process, they share NATS connections and possibly other services.

### ServicesProcess
A `ServicesProcess` class is needed - one instance per process that every service knows about. If there's only one service in the process, `ServicesProcess` still exists. Could be implemented as a Singleton to ensure this.

### ServiceManager
Can be overridden, but every service always has some manager, usually not in its own process.

### ServiceLauncher
Overridden by specific launchers, knows the Service Manager and delegates service start/stop operations to it. Service instances without launchers can exist (manually started) but they still have managers (they create a default one).

## Preventing Redundancy

The entire project should avoid multiplying mandatory elements, names, etc. Implementing a `HelloWorld` service must be super simple.

## Minimal Dependencies

Services at the base level use NATS through `serverish.Messenger`. Services are asynchronous in the sense that overridden methods are async.

## Implementation Matrix (TODO)

Prepare and diagram an implementation functionality matrix for use cases. Axes:
* Permanent/periodic service
* Service from `ocabox_tcs.services` or external project
* Manual/process/asyncio/systemd execution
* Various configuration methods - configuration file locations

For each combination, we need to know how to implement startup/shutdown, management, and configuration.