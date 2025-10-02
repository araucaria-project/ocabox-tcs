
# OCM Telescope Control Services (ocabox-tcs)

Collection of automation services for OCM telescopes.

## Installation

### Prerequisites

* Python 3.10+
* poetry
* systemd-based Linux (e.g. Ubuntu 22.04 LTS) (for dev, macOS is also supported)
* NATS server running (nats.oca.lan in observatory)
* TIC (`ocabox-server`) server running (for services controlling the telescope)

### Installation Steps

1. Clone the repository:
```bash
cd ~/src
git clone https://github.com/araucaria-project/ocabox-tcs.git
cd ocabox-tas
```

2. Install dependencies:
```bash
poetry install
```

3. Configuration:
Configuration files are located in the `config/` directory of the project.

**Create your configuration file:**
```bash
# Copy sample configuration
cp config/services.sample.yaml config/services.yaml

# Edit configuration as needed
# config/services.yaml is gitignored - customize for your environment
```

**Available example configurations:**
```bash
ls config/
# services.sample.yaml - Main configuration template
# examples.yaml - Tutorial examples configuration
```

4. Install systemd services:
```bash
# Create symlinks for systemd services
sudo ln -s ~/src/ocabox-tcs/scripts/ocabox-services-launcher.service /etc/systemd/system/
sudo ln -s ~/src/ocabox-tcs/scripts/ocabox-service@.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable ocabox-services-launcher
sudo systemctl start ocabox-services-launcher
```

### Service Control

Services can be controlled either through `oca` CLI:
```bash
# Start plan runner for ZB08
oca tas start plan_runner zb08

# Check status
oca tas status
```

Or directly through systemctl:
```bash
systemctl start ocabox-service@plan_runner-zb08.service
systemctl status ocabox-service@plan_runner-zb08.service
```

## Development

### Development without systemd (e.g. macOS)

For development on macOS where systemd is not available:

1. Install dependencies:
```bash
poetry install
```
Run development service managers:
```bash
# Process launcher (separate processes)
poetry run tcs_process --config config/services.yaml

# Asyncio launcher (same process)
poetry run tcs_asyncio --config config/services.yaml

# Tutorial examples
poetry run tcs_asyncio --config config/examples.yaml
```
These launchers will start services defined in config files instead of using systemd.
For full testing before release, use PyCharm Professional's remote development feature to develop and test directly on observatory machine.

### Project structure
```
ocabox-tcs/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── doc/
│   ├── development-guide.md
│   ├── architecture.md
│   └── requirements-analysis.md
├── config/
│   ├── services.sample.yaml   # Configuration template
│   └── examples.yaml           # Tutorial examples
├── scripts/
│   ├── ocabox-services-launcher.service
│   └── ocabox-service@.service
└── src/
    └── ocabox_tcs/
        ├── __init__.py
        ├── base_service.py
        ├── launchers/
        │   ├── process.py          # Process launcher
        │   ├── asyncio.py          # Asyncio launcher
        │   └── base_launcher.py
        ├── services/
        │   ├── __init__.py
        │   ├── hello_world.py
        │   └── examples/           # Tutorial examples
        │       ├── 01_minimal.py
        │       ├── 02_basic.py
        │       ├── 03_logging.py
        │       ├── 04_monitoring.py
        │       └── README.md       # ← Start here!
        └── management/
            ├── process_context.py
            ├── service_controller.py
            └── configuration.py
```

## Architecture

### Universal Service Framework

This project provides a universal Python service framework for telescope automation:

- **Execution Independence**: Services work with any execution method (manual, subprocess, asyncio, systemd, containers)
- **Service Types**: Supports permanent, blocking permanent, and single-shot services
- **Decorator-Based**: Modern Python decorators for clean service registration
- **Optional Configuration**: Config classes are optional, services can use base config
- **Distributed Management**: NATS-based service discovery, lifecycle management, and health monitoring
- **Flexible Deployment**: Services can be local or from external packages

### Services

Services are individual components that perform specific automation tasks. Each service:

- Uses `@service` decorator for registration (service type derived from filename)
- Inherits from `BasePermanentService`, `BaseBlockingPermanentService`, or `BaseSingleShotService`
- Implements `async def start_service()` and `async def stop_service()` (or specialized methods)
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

## Quick Start

### For Learning the Framework

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

See [Tutorial Examples README](src/ocabox_tcs/services/examples/README.md) for detailed walkthrough.

### For Development/Testing

1. Install dependencies:
```bash
poetry install
```

2. Create a service file `my_service.py`:
```python
from ocabox_tcs.base_service import service, BaseBlockingPermanentService

@service
class MyService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            self.logger.info("Service running...")
            await asyncio.sleep(5)
```

3. Add to config file (use filename as service type):
```yaml
services:
  - type: my_service    # Must match filename
    instance_context: main
```

4. Run with launchers:
```bash
# Services in separate processes
poetry run tcs_process --config config/services.yaml

# Services in same process
poetry run tcs_asyncio --config config/services.yaml
```

5. Or run service directly:
```bash
python my_service.py config/services.yaml main
```

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

## Starting and Stopping Services

### Manual start of single service
Start service file from command line.
```commandline
usage: hello_world.py [-h] [--runner-id RUNNER_ID] config_file instance_context

Start a TCS service.

positional arguments:
  config_file       Path to the config file
  instance_context  Service instance context/ID

options:
  -h, --help        show this help message and exit
  --runner-id RUNNER_ID
                    Optional runner ID for monitoring
```

The service type is automatically derived from the filename (e.g. `hello_world.py` → service type `hello_world`).
The `instance_context` must match an entry in the `services.yaml` configuration file.

### Start all services from config file

**As separate processes:**
```bash
poetry run tcs_process --config config/services.yaml
```

**In same process (asyncio):**
```bash
poetry run tcs_asyncio --config config/services.yaml
```
