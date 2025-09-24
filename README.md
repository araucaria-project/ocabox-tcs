
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

3. Create configuration:
```bash
sudo mkdir -p /etc/ocabox
sudo cp config/tcs.yaml.example /etc/ocabox/tcs.yaml
```
Edit `/etc/ocabox/tcs.yaml` according to your needs.

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
Run development service manager:
```bash
poetry run tas_dev
```
This will start services defined in config/services.yaml using subprocesses instead of systemd.
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
│   └── services.yaml.example
├── scripts/
│   ├── ocabox-services-launcher.service
│   └── ocabox-service@.service
└── src/
    └── ocabox_tcs/
        ├── __init__.py
        ├── base_service.py
        ├── launcher.py
        ├── services/
        │   ├── __init__.py
        │   ├── plan_runner.py
        │   ├── guider.py
        │   └── dome_follower.py
        └── cli.py
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

- Uses `@service("name")` decorator for registration
- Inherits from `BasePermanentService`, `BaseBlockingPermanentService`, or `BaseSingleShotService`
- Implements `async def start_service()` and `async def stop_service()` (or specialized methods)
- Optionally uses `@config("name")` decorator for custom configuration
- Gets automatic NATS integration, health checking, and management

**Example Service**:
```python
from ocabox_tcs.base_service import service, BasePermanentService

@service("hello_world")
class HelloWorldService(BasePermanentService):
    async def start_service(self):
        self.logger.info("Hello World!")
```

Services are defined in `config/services.yaml` and can be launched via multiple methods depending on deployment needs.

## Quick Start

For development/testing:

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

4. Run with launcher:
```bash
poetry run tcs_process  # Services in separate processes
# or
poetry run tcs_asyncio  # Services in same process
```

5. Or run service directly:
```bash
python my_service.py config.yaml main
```

## Documentation

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
Start service file crom commandline.
```commandline
usage: dumb_permanent.py [-h] config_file service_type service_id

Start an OCM automation service.

positional arguments:
  config_file   Path to the config file
  service_type  Type of the service - module name
  service_id    Service instance context/ID

options:
  -h, --help    show this help message and exit
```

where `service_type` is the name of the service module (e.g. `plan_runner`) and `service_id` is the instance ID (e.g. `dev`).
Those names must match the names in the `services.yaml` configuration file.

### Start all services from config file as separate processes

```bash
 poetry run tcs_dev
```
