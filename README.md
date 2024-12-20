
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

## Concepts

### Services

Services are individual components that perform specific tasks. They are defined in `config/services.yaml` file
(or equivalent file from another location).

Services are able to run independently and can be started, stopped, and restarted individually, but normally, 
they are managed by a service launcher that ensures they are running and restarts them if they fail.

For development, `dev_launcher.py` is used to run services as subprocesses. In production, services are managed 
by systemd, and `systemd_launcher.py` is used to start, stop and manage services.

When implementing a new service, it should be added to `services` directory and registered in `services.yaml`.
The service class should inherit from `BaseService` and implement `start`, `stop`, and `restart` methods.

