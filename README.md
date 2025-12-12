# OCM Telescope Control Services (ocabox-tcs)

Collection of automation services for OCM telescopes.

## Installation

### Prerequisites

* Python 3.12+
* poetry (for development)
* NATS server running (e.g. `nats.oca.lan` in observatory, or `localhost` for development)
* TIC (`ocabox-server`) server running (only required for services controlling the telescope)

### Installation Steps

#### For Library Use Only

The package is not published on PyPI. Install directly from the GitHub repository:

```bash
# stable (default branch)
pip install git+https://github.com/araucaria-project/ocabox-tcs.git

# install with the optional CLI extras (wrap in quotes to include extras)
pip install "git+https://github.com/araucaria-project/ocabox-tcs.git#egg=ocabox-tcs[cli]"
```

Notes:
- Use the `@<branch-or-tag>` suffix if you need a specific branch or tag, e.g.
  `pip install git+https://github.com/araucaria-project/ocabox-tcs.git@main`.
- If you prefer an editable/dev install after cloning the repo, see the Development section below.

#### Requirements.txt (old-school projects)

Add this line to your requirements.txt to pin the repo as a dependency:

```
git+https://github.com/araucaria-project/ocabox-tcs.git#egg=ocabox-tcs
```

To include CLI extras in requirements.txt:

```
git+https://github.com/araucaria-project/ocabox-tcs.git#egg=ocabox-tcs[cli]
```

#### Editable / Development install (pip + venv)

If you're developing or want an editable install:

```bash
# clone first
git clone https://github.com/araucaria-project/ocabox-tcs.git
cd ocabox-tcs

# optional: create & activate a venv
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# install editable (includes extras if desired)
pip install -e .           # library only
pip install -e ".[cli]"    # include CLI extras
```

#### Poetry

Add directly from the repo:

```bash
poetry add git+https://github.com/araucaria-project/ocabox-tcs.git
```

Or add to pyproject.toml dependencies:

```toml
[tool.poetry.dependencies]
# point to the repository (lock to branch/tag with 'rev' if desired)
ocabox-tcs = { git = "https://github.com/araucaria-project/ocabox-tcs.git", rev = "main" }
```

#### uv (pyproject / PEP 621)

If your project is managed with "uv" (or any tool that uses pyproject.toml / PEP‑621), add ocabox-tcs as a direct VCS dependency using a PEP‑508 direct URL. This works with tools that read [project].dependencies.

Example (pyproject.toml):

```toml
[project]
dependencies = [
  # point to repository, lock to branch/tag/commit with @<rev>
  "ocabox-tcs @ git+https://github.com/araucaria-project/ocabox-tcs.git@main",

  # include extras if needed
  "ocabox-tcs[cli] @ git+https://github.com/araucaria-project/ocabox-tcs.git@main"
]
```

Notes:
- Replace `@main` with a tag (e.g. `@v1.2.3`) or commit SHA to pin a revision.
- If your tool instead expects a requirements-style file, use the `git+https://...#egg=...` form shown in the Requirements.txt section.

#### Pipenv

Install from the repo with pipenv:

```bash
pipenv install -e "git+https://github.com/araucaria-project/ocabox-tcs.git#egg=ocabox-tcs"
```

### For Development

1. Clone the repository:
```bash
cd ~/src
git clone https://github.com/araucaria-project/ocabox-tcs.git
cd ocabox-tcs
```

2. Install dependencies (includes CLI tools automatically):
```bash
poetry install
```

3. Create configuration:
```bash
# Copy sample configuration
cp config/services.sample.yaml config/services.yaml

# Edit configuration as needed
# config/services.yaml is gitignored - customize for your environment
```

## Quick Start: Running Services

### Using Launchers

Run services using the provided launchers:

```bash
# Process launcher (separate processes, recommended for production)
poetry run tcs_process --config config/services.yaml

# Asyncio launcher (same process, faster iteration for development)
poetry run tcs_asyncio --config config/services.yaml

# Tutorial examples
poetry run tcs_asyncio --config config/examples.yaml
```

**For production deployment**: You can create your own systemd service that runs `tcs_process` or `tcs_asyncio` with your configuration. The framework itself is deployment-agnostic.

### Manual Service Launch

Start a single service directly:

```bash
poetry shell  # if needed to activate venv
python src/ocabox_tcs/services/hello_world.py config/services.yaml main
```

Usage pattern: `python service_file.py config_file instance_context [--runner-id RUNNER_ID]`

### Monitoring Running Services

**Using `tcsctl` CLI:**
```bash
# List running services
tcsctl

# Show all services including stopped
tcsctl --all

# Detailed view with metadata
tcsctl --detailed

# Filter specific service
tcsctl hello_world

# Show collection statistics
tcsctl --verbose

# Show legend
tcsctl --legend
```

## Quick Start: Developing Services

### Learning the Framework

**New to ocabox-tcs?** Start with the tutorial examples:

```bash
# View the Getting Started guide
cat src/ocabox_tcs/services/examples/README.md

# Run all tutorial examples
poetry run tcs_asyncio --config config/examples.yaml
```

The tutorial includes:
- `01_minimal.py` - Simplest possible service (< 30 lines)
- `02_basic.py` - Service with configuration
- `03_logging.py` - Logging best practices
- `04_monitoring.py` - Monitoring and health checks
- `05_nonblocking.py` - Non-blocking service with background tasks

See [Tutorial Examples README](src/ocabox_tcs/services/examples/README.md) for detailed walkthrough.

### Creating Your First Service

1. Create a service file `my_service.py`:
```python
from ocabox_tcs.base_service import service, BaseBlockingPermanentService

@service
class MyService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            self.logger.info("Service running...")
            await asyncio.sleep(5)
```

2. Add to config file (use filename as service type):
```yaml
services:
  - type: my_service    # Must match filename
    instance_context: main
```

3. Run it:
```bash
poetry run tcs_asyncio --config config/services.yaml
```

For detailed guidance on choosing base classes and implementing services, see [Development Guide](doc/development-guide.md).

## Quick Start: Adding Monitoring to Your Project

The monitoring system can be used in **any Python project** that needs status reporting over NATS, not just TCS services.

### Basic Usage (Simple)

Add monitoring to your application with automatic lifecycle management:

```python
from ocabox_tcs.monitoring import create_monitor, Status
from serverish.messenger import Messenger

async def main():
    # You manage Messenger (your existing setup)
    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        # create_monitor auto-discovers Messenger automatically!
        monitor = await create_monitor('my_app', subject_prefix='myproject')

        async with monitor:
            monitor.set_status(Status.OK, "Application started")
            await do_work()
```

That's it! Your application gets:
- Status reporting to NATS stream `myproject.status.my_app`
- Heartbeat monitoring (10s) on `myproject.heartbeat.my_app`
- Lifecycle events on `myproject.registry.*`
- Automatic healthcheck loop (30s)

**Note**: `create_monitor()` auto-detects NATS availability. If NATS is not available, it returns a no-op monitor for graceful degradation.

### Advanced: Health Check Callbacks

Add custom health checks that run periodically (default: every 30s):

```python
from ocabox_tcs.monitoring import create_monitor, Status
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context():
        monitor = await create_monitor(name="my_app")
        monitor.add_healthcheck_cb(check_health)

        async with monitor:
            await do_work()

def check_health() -> Status | None:
    """Called every 30s by healthcheck loop"""
    # Return Status.DEGRADED if unhealthy, None if healthy
    return None  # No opinion = healthy
```

### Advanced: Metric Collection

Collect custom metrics in status reports:

```python
from ocabox_tcs.monitoring import create_monitor
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context():
        monitor = await create_monitor(name="my_app")
        monitor.add_metric_cb(get_metrics)

        async with monitor:
            await do_work()

def get_metrics() -> dict:
    """Called when generating status reports"""
    return {
        "queue_size": 10,
        "errors": 0,
        "processed": 1000
    }
```

### Advanced: Task Tracking (BUSY/IDLE)

Track task execution with automatic BUSY/IDLE status:

```python
from ocabox_tcs.monitoring import create_monitor
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context():
        monitor = await create_monitor(name="my_app")

        async with monitor:
            await process_item("data")

async def process_item(item):
    # Automatic status management:
    # - Immediately switches to BUSY
    # - Waits 1s after task ends before IDLE
    # - Cancels IDLE transition if new task starts
    async with monitor.track_task():
        await do_processing(item)
```

The monitoring system handles all NATS communication, status transitions, and heartbeat publishing automatically.

### Advanced: Creating Service Hierarchies

For applications that spawn child processes or manage subcomponents, you can create hierarchical displays in `tcsctl` using the `parent_name` parameter. This is especially useful for:
- Services that launch helper subprocesses
- Applications managing worker pools
- Main servers coordinating with tool processes (e.g., MCP servers, language servers)

**How It Works:**
1. Parent process passes its **own monitor name** to child as `--parent-name` CLI argument
2. Child process uses this value when creating its monitor
3. `tcsctl` displays the hierarchy automatically (supports unlimited depth)

**Example: Service Launching Subprocesses**

```python
# parent_service.py - Main service
from ocabox_tcs.base_service import service, BaseBlockingPermanentService
import asyncio

@service
class ParentService(BaseBlockingPermanentService):
    async def on_start(self):
        """Launch child processes with parent tracking."""

        # STEP 1: Get your own monitor name
        my_name = self.controller.monitor.name
        # For service "parent_service:dev", this is: "parent_service:dev"

        # STEP 2: Pass your name to child as --parent-name
        self.child_process = await asyncio.create_subprocess_exec(
            "python", "-m", "my_package.child_service",
            "config.yaml",
            "dev",
            "--parent-name", my_name,  # ← Declare parent relationship
        )
```

```python
# child_service.py - Child subprocess
from ocabox_tcs.base_service import service, BaseBlockingPermanentService

@service
class ChildService(BaseBlockingPermanentService):
    # Child receives --parent-name via CLI args
    # TCS framework automatically handles this and sets monitor.parent_name

    async def run_service(self):
        while self.is_running:
            await asyncio.sleep(1)
```

**Result in tcsctl:**
```bash
$ tcsctl list
◐ parent_service:dev [ok] (♥) up:5m - Main service
  ├─ child_service:dev [ok] (♥) up:5m - Child process
```

**Three-Level Hierarchy (Launcher → Service → Subprocess):**

When using a TCS launcher, you automatically get a third level:

```bash
$ poetry run tcs_asyncio --config config/services.yaml
$ tcsctl list
◐ launcher.asyncio-launcher.hostname-abc123 [ok] (♥) up:10m - Launcher
  ├─ parent_service:dev [ok] (♥) up:10m - Main service
      ├─ worker_1:dev [ok] (♥) up:10m - Worker process 1
      └─ worker_2:dev [ok] (♥) up:10m - Worker process 2
```

**For Non-TCS Applications:**

If you're not using TCS `BaseService`, manually pass `parent_name` when creating the monitor:

```python
import argparse
from ocabox_tcs.monitoring import create_monitor

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-name", default=None)
    args = parser.parse_args()

    # Create monitor with parent_name
    monitor = await create_monitor(
        name="my_worker:dev",
        parent_name=args.parent_name,  # From CLI argument
    )

    async with monitor:
        await do_work()
```

**Key Points:**
- `parent_name` is **optional** - entities without it appear as top-level
- Parent passes **its own `monitor.name`** to children
- For TCS services: Access via `self.controller.monitor.name`
- Supports **unlimited hierarchy depth** (2, 3, 4+ levels)
- Only affects **display** in `tcsctl` - doesn't change process relationships

For complete guidelines, see [doc/parent-name-guidelines.md](doc/parent-name-guidelines.md).

## Quick Start: Writing Monitoring UI

Use `ServiceControlClient` to build custom monitoring interfaces, dashboards, or GUIs.

**Note**: A NATS `Messenger` instance must be open before using `ServiceControlClient`. Open it at your application's top level with `async with messenger.context()`.

### One-Shot Snapshot

```python
from tcsctl import ServiceControlClient
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        client = ServiceControlClient(messenger, subject_prefix='svc')
        services = await client.list_services(include_stopped=False)

        for service in services:
            print(f"{service.service_id}: {service.status.value}")
            print(f"  Uptime: {service.uptime_seconds}s")
            print(f"  Heartbeat: {service.last_heartbeat_age}s ago")
```

### Streaming Mode (Real-Time Updates)

```python
async def main():
    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        client = ServiceControlClient(messenger, subject_prefix='svc')

        def on_update(service_info):
            """Called whenever a service status changes"""
            print(f"Updated: {service_info.service_id} -> {service_info.status}")

        client.on_service_update = on_update
        await client.start_following()

        # Access current state anytime
        services = client.get_current_services()

        # Keep running...
        await asyncio.sleep(60)
        await client.stop_following()
```

### Building Custom Displays

The `ServiceControlClient` provides:
- Real-time service discovery
- Status updates via callbacks
- Hierarchical service relationships (launcher → services)
- Heartbeat monitoring and zombie detection

See `examples/monitoring_client_usage.py` for complete examples including:
- Terminal UIs with Rich
- Web dashboards
- Integration with existing monitoring tools

## Project Structure
```
ocabox-tcs/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── doc/
│   ├── development-guide.md    # Service development guide
│   ├── architecture.md         # Technical architecture
│   ├── requirements-analysis.md
│   └── feature-roadmap.md      # Planned features
├── config/
│   ├── services.sample.yaml    # Configuration template
│   └── examples.yaml           # Tutorial examples
└── src/
    ├── ocabox_tcs/             # Core service framework
    │   ├── __init__.py
    │   ├── base_service.py
    │   ├── launchers/
    │   │   ├── process.py      # Process launcher
    │   │   ├── asyncio.py      # Asyncio launcher
    │   │   └── base_launcher.py
    │   ├── services/
    │   │   ├── __init__.py
    │   │   ├── hello_world.py
    │   │   └── examples/       # Tutorial examples
    │   │       ├── 01_minimal.py
    │   │       ├── 02_basic.py
    │   │       ├── 03_logging.py
    │   │       ├── 04_monitoring.py
    │   │       ├── 05_nonblocking.py
    │   │       └── README.md   # ← Start here!
    │   ├── management/
    │   │   ├── process_context.py
    │   │   ├── service_controller.py
    │   │   └── configuration.py
    │   └── monitoring/
    │       ├── status.py
    │       ├── monitored_object.py
    │       └── monitored_object_nats.py
    └── tcsctl/                 # CLI tool (optional)
        ├── __init__.py
        ├── app.py              # Main entry point
        ├── client.py           # ServiceControlClient
        ├── display.py          # Rich terminal output
        └── commands/
            └── list.py         # List command
```

## Architecture

### Universal Service Framework

This project provides a universal Python service framework for telescope automation:

- **Execution Independence**: Services are designed to work with multiple execution methods:
  - **Currently available**: Manual launch, process launcher (`tcs_process`), asyncio launcher (`tcs_asyncio`)
  - **Planned**: Systemd integration, container deployment (see [Feature Roadmap](doc/feature-roadmap.md))
- **Service Types**: Supports permanent and blocking permanent services (single-shot planned)
- **Decorator-Based**: Modern Python decorators for clean service registration
- **Optional Configuration**: Config classes are optional, services can use base config
- **Distributed Management**: NATS-based service discovery, lifecycle management, and health monitoring
- **Flexible Deployment**: Services can be local or from external packages (external packages planned)

### Services

Services are individual components that perform specific automation tasks. Each service:

- Uses `@service` decorator for registration (service type derived from filename)
- Inherits from `BasePermanentService` or `BaseBlockingPermanentService`
  - `BaseSingleShotService` exists but runner support is planned (see [Feature Roadmap](doc/feature-roadmap.md#4-single-shot-and-cyclic-services))
- Implements `async def start_service()` and `async def stop_service()` (or `async def run_service()` for blocking services)
- Optionally uses `@config` decorator for custom configuration
- Gets automatic NATS integration, health checking, and management

**Example Service** (File: `hello_world.py`):
```python
from ocabox_tcs.base_service import service, BasePermanentService

@service
class HelloWorldService(BasePermanentService):
    async def start_service(self):
        self.logger.info("Hello World!")
```

Services are defined in `config/services.yaml` and can be launched via multiple methods depending on deployment needs.

## Configuration

### Configuration System Overview

The universal service framework supports multiple configuration sources with clear precedence:

1. **Command-line arguments** (highest priority)
2. **NATS configuration** (planned, not implemented yet)
3. **YAML config file** (specified via CLI)
4. **Default values** (lowest priority)

### Configuration File Structure

**Location**: `config/services.yaml` (created by copying `config/services.sample.yaml`)

```yaml
# Global configuration (applies to all services)
nats:
  host: nats.oca.lan
  port: 4222

# Service-specific configuration
services:
  - type: hello_world         # Must match service filename (hello_world.py)
    instance_context: main    # Instance identifier
    interval: 5              # Service-specific config options
    message: "Hello World!"  # Service-specific config options
    log_level: INFO          # Optional log level override

  - type: hello_world         # Same service, different instance
    instance_context: fast   # Different instance identifier
    interval: 1              # Different configuration values
    message: "Fast hello!"
    log_level: DEBUG
```

### Configuration Resolution

1. **Service Type**: Automatically derived from filename (`hello_world.py` → `hello_world`)
2. **Instance Matching**: Finds service entry with matching `type` and `instance_context`
3. **Config Merging**: Global config is merged with service-specific config
4. **Precedence**: Service-specific values override global values

### Available Configuration Files

- `config/services.sample.yaml` - Template for main configuration (copy to `services.yaml`)
- `config/services.yaml` - Your customized configuration (gitignored, create from sample)
- `config/examples.yaml` - Tutorial examples configuration

### Environment Variables and Secrets Management

**Problem**: Configuration files should never contain secrets (API keys, passwords, etc.) that could be accidentally committed to version control.

**Solution**: Use environment variable expansion in YAML files and `.env` files for development.

#### Using Environment Variables in Configuration

You can use `${VAR_NAME}` syntax in any YAML configuration file:

```yaml
# config/services.yaml
nats:
  host: "${NATS_HOST}"    # String: "localhost" → "localhost"
  port: ${NATS_PORT}      # Auto-converts: "4222" → 4222 (int)

services:
  - type: my_service
    instance_context: prod
    api_key: "${API_KEY}"              # Replaced with env var value
    database_url: "${DATABASE_URL}"    # Supports any config field
    timeout: 30                        # Regular values work as before
```

**Behavior:**
- Defined variables are replaced with their values at load time
- Undefined variables keep the placeholder `${UNDEFINED_VAR}` and log a warning
- **Automatic type conversion**: Pure variable references like `port: ${NATS_PORT}` auto-convert numeric strings to int/float
- Works recursively in dictionaries and lists
- Only alphanumeric + underscore variable names supported: `${VAR_NAME_123}`

#### Development: Using .env Files

For local development, create a `.env` file in the project root:

```bash
# 1. Copy the template
cp .env.example .env

# 2. Edit with your secrets (never commit this file!)
cat .env
API_KEY=sk-your-key-here
DATABASE_URL=postgresql://localhost/mydb
ANTHROPIC_API_KEY=sk-ant-...

# 3. Run launcher (automatically loads .env)
poetry run tcs_asyncio --config config/services.yaml
```

**Notes:**
- `.env` is automatically loaded by launchers and standalone services
- Existing environment variables take precedence over `.env` values
- `.env` is in `.gitignore` (safe from accidental commits)
- Use `.env.example` as a template for your team

#### Production: Environment Variables

For production deployment (systemd, Docker, etc.), set environment variables directly:

**Systemd Example:**
```ini
[Service]
Environment="API_KEY=your-production-key"
EnvironmentFile=/etc/ocabox-tcs/secrets.env
ExecStart=/usr/bin/poetry run tcs_process --config /etc/ocabox-tcs/services.yaml
```

**Docker Example:**
```bash
docker run -e API_KEY=your-key -e DATABASE_URL=postgresql://... my-service
```

**Security Best Practices:**
- ✅ Use environment variables for all secrets
- ✅ Use `.env` files for development only
- ✅ Keep `.env` in `.gitignore`
- ✅ Provide `.env.example` template for team setup
- ❌ Never commit secrets to version control
- ❌ Never use `.env` files in production

## Documentation

### [Tutorial Examples](src/ocabox_tcs/services/examples/README.md) 📚 **START HERE**
**Getting Started Guide**
- Progressive examples from simple → complex
- Step-by-step instructions
- Copy-paste ready code
- Best for learning the framework

### [Development Guide](doc/development-guide.md)
**User Guide for Service Development**
- Decision tree for choosing the right base class
- Examples and best practices
- Migration patterns from old code
- Quick reference for implementing services

Use this when you need to create a new service or understand how to implement service functionality.

### [Architecture](doc/architecture.md)
**Technical Architecture Documentation**
- Comprehensive framework design
- Implementation details for the universal service framework
- Component relationships and interactions
- Detailed implementation matrix

Use this when you need to understand the technical design or modify framework internals.

### [Requirements Analysis](doc/requirements-analysis.md)
**Original Requirements and Design Analysis**
- Translated from original Polish planning document
- Requirements gathering and analysis
- Design decisions and rationale
- Historical context for architecture choices

Use this to understand why design decisions were made and the original requirements that drove the architecture.

### [Claude Instance Guide](CLAUDE.md)
Instructions for Claude instances working on this project
