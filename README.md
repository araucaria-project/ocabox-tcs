# OCM Telescope Control Services (ocabox-tcs)

Universal Python service framework for OCM telescope automation. Services get NATS integration, lifecycle management, health monitoring, and crash-recovery for free; they are decorator-registered and execution-method agnostic.

## Prerequisites

- Python 3.12+
- NATS server (e.g. `nats.oca.lan` in observatory, `localhost` for development)
- Poetry (for development) or pip
- TIC (`ocabox-server`) — only for services that talk to the telescope

## Installation

### Library (consumers)

```bash
# Library only
pip install git+https://github.com/araucaria-project/ocabox-tcs.git

# With CLI tools (tcsctl)
pip install "git+https://github.com/araucaria-project/ocabox-tcs.git#egg=ocabox-tcs[cli]"
```

Add `@<branch-or-tag>` to pin a revision. Same URL works in `requirements.txt`, `pyproject.toml` (`[project].dependencies`), Poetry, uv, and pipenv.

### Development

```bash
git clone https://github.com/araucaria-project/ocabox-tcs.git
cd ocabox-tcs
poetry install                                     # all dev deps + CLI
cp config/services.sample.yaml config/services.yaml   # gitignored — customize
```

## Running services

Use `tcsd` — the unified launcher. Defaults: `config/services.yaml`, asyncio mode, Rich-colored logs.

```bash
poetry run tcsd                                    # default config & mode
poetry run tcsd --config config/services.yaml     # explicit config
poetry run tcsd --launcher process                # each service as a subprocess
poetry run tcsd --no-color                        # plain text logs
```

**Launcher modes** (`--launcher`):
- `asyncio` (default) — all services in one process, lower overhead, easier debugging
- `process` — each service as a subprocess, better isolation, higher overhead

For production, run `tcsd` from a systemd unit (the framework itself is deployment-agnostic). See [Restart Policies Guide](doc/restart-policies.md) for crash-recovery configuration.

### Direct service launch (development)

To iterate on a single service without booting the whole launcher, run the service file directly:

```bash
python src/ocabox_tcs/services/hello_world.py                                 # all defaults
python src/ocabox_tcs/services/hello_world.py prod                            # custom variant
python src/ocabox_tcs/services/hello_world.py config/services.yaml prod       # explicit config + variant
```

Pattern: `python service_file.py [config_file] [variant] [--runner-id ID]`. All arguments are optional — defaults: no config (uses `localhost:4222`), variant `dev`.

## Monitoring

```bash
tcsctl                              # running services + recently-broken
tcsctl --all                        # include stopped/old declared
tcsctl --detailed                   # multi-line view with metadata
tcsctl hello_world                  # filter by service name (substring)
tcsctl --legend                     # symbol legend
tcsctl --host nats.oca.lan          # override config (otherwise read from services.yaml)
```

`tcsctl` reads NATS host/port/prefix from `config/services.yaml` (with `.env` and `NATS_HOST`/`NATS_PORT` fallbacks). CLI flags override.

## Writing a service

Start with the tutorial: [`src/ocabox_tcs/services/examples/README.md`](src/ocabox_tcs/services/examples/README.md) — five progressive examples from a 30-line minimum to non-blocking with background workers. Run them via `tcsd --config config/examples.yaml`.

Minimal blocking service:

```python
# my_service.py — service type derived from filename
import asyncio
from ocabox_tcs.base_service import service, BaseBlockingPermanentService

@service
class MyService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            self.svc_logger.info("tick")
            await asyncio.sleep(5)
```

Add to config:

```yaml
services:
  - type: my_service       # must match filename
    variant: main
```

Run: `poetry run tcsd`. See [Development Guide](doc/development-guide.md) for choosing base classes and patterns.

## Configuration

| Source                       | Priority | Use case                          |
|------------------------------|----------|-----------------------------------|
| Command-line args            | highest  | Overrides for one run             |
| YAML file                    | high     | Production, multi-service         |
| Env vars `{SERVICE}_{FIELD}` | medium   | External projects, secrets        |
| `@config` class defaults     | lowest   | Code defaults                     |

```yaml
# config/services.yaml
nats:
  host: "${NATS_HOST}"     # ${VAR} and ${VAR:-default} expansion
  port: 4222

services:                  # YAML list (each item with `-`)
  - type: my_service
    variant: prod
    api_key: "${API_KEY}"
```

**NATS connection**: `nats:` section, or `NATS_HOST`/`NATS_PORT` env vars (defaults `localhost:4222`).

**Service config without YAML**: env vars `{SERVICE_TYPE}_{FIELD}` (uppercase) — e.g. `MY_SERVICE_API_KEY=secret` for field `api_key` in `my_service.py`. Auto-converts numbers and booleans. `.env` is auto-loaded.

Files: `config/services.sample.yaml` (template), `config/services.yaml` (yours, gitignored), `.env` (gitignored).

## Embedding monitoring in any Python app

The monitoring system works in any project that needs status reporting over NATS — not just TCS services.

```python
from ocabox_tcs.monitoring import create_monitor, Status
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        monitor = await create_monitor('my_app', subject_prefix='myproject')
        async with monitor:
            monitor.set_status(Status.OK, "Application started")
            await do_work()
```

This publishes status, heartbeats (10 s), and lifecycle events on `myproject.{status,heartbeat,registry}.my_app`. If NATS is unavailable, `create_monitor` returns a no-op monitor for graceful degradation.

Add custom logic via `monitor.add_healthcheck_cb(...)` (periodic `Status` decisions), `monitor.add_metric_cb(...)` (custom fields in status reports), or `async with monitor.track_task(): ...` (automatic BUSY/IDLE).

For hierarchical display in `tcsctl` (parent → service → child), pass `parent_name=<parent's monitor.name>` to children — see [doc/parent-name-guidelines.md](doc/parent-name-guidelines.md).

## Building monitoring UIs

`ServiceControlClient` exposes the snapshot/streaming API used by `tcsctl`:

```python
from tcsctl import ServiceControlClient
from serverish.messenger import Messenger

async def main():
    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        client = ServiceControlClient(messenger, subject_prefix='svc')

        # Snapshot
        for s in await client.list_services():
            print(f"{s.service_id}: {s.status.value}")

        # Streaming with callbacks
        client.on_service_update = lambda info: print(f"upd: {info.service_id}")
        await client.start_following()
        # ... client.get_current_services() any time ...
        await client.stop_following()
```

See `examples/monitoring_client_usage.py` for terminal UIs (Rich), web dashboards, and integrations.

## Documentation

- **[Tutorial Examples](src/ocabox_tcs/services/examples/README.md)** — start here
- [Development Guide](doc/development-guide.md) — base classes, patterns, migration
- [Restart Policies Guide](doc/restart-policies.md) — crash recovery configuration
- [Architecture](doc/architecture.md) — framework internals
- [Requirements Analysis](doc/requirements-analysis.md) — design rationale
- [Feature Roadmap](doc/feature-roadmap.md) — planned work
- [CLAUDE.md](CLAUDE.md) — instructions for Claude instances working on this project
