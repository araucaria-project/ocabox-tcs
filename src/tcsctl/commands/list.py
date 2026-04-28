"""List command for tcsctl."""

import asyncio
import logging
from typing import Annotated

import typer
from serverish.messenger import Messenger

from ocabox_tcs.management.bootstrap import (
    NatsSettings,
    determine_config_file,
    resolve_nats_settings,
)
from ocabox_tcs.management.configuration import ConfigurationManager, FileConfigSource
from ocabox_tcs.management.environment import load_dotenv_if_available
from tcsctl.client import ServiceControlClient
from tcsctl.display import display_services_table, display_services_detailed, display_legend


async def _list_services_async(
    all: bool = False,
    detailed: bool = False,
    service: str | None = None,
    host: str = "localhost",
    port: int = 4222,
    subject_prefix: str = "svc",
    timeout: float = 5.0
):
    """Async implementation of list_services with timeout.

    Args:
        timeout: Connection timeout in seconds (default: 5.0)
    """
    messenger = Messenger()

    # Wrap in timeout to prevent hanging
    async with asyncio.timeout(timeout):
        async with messenger.context(host=host, port=port):
            client = ServiceControlClient(messenger, subject_prefix=subject_prefix)
            services = await client.list_services(include_stopped=all)

    # Display results
    if detailed:
        display_services_detailed(services, show_all=all, service_filter=service)
    else:
        display_services_table(services, show_all=all, service_filter=service)


def _run_list(
    nats_settings: NatsSettings,
    all: bool,
    detailed: bool,
    service: str | None,
):
    """Open NATS using resolved settings and dispatch to display."""
    try:
        asyncio.run(_list_services_async(
            all=all,
            detailed=detailed,
            service=service,
            host=nats_settings.host,
            port=nats_settings.port,
            subject_prefix=nats_settings.subject_prefix,
        ))
    except TimeoutError:
        typer.secho(
            f"Timeout connecting to NATS at {nats_settings.host}:{nats_settings.port}. "
            "Is NATS server running?",
            fg=typer.colors.RED, err=True
        )
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"Error connecting to NATS: {e}", fg=typer.colors.RED, err=True)
        raise


def list_services_cmd(
    service: Annotated[str | None, typer.Argument(help="Filter by service name (substring match)")] = None,
    all: Annotated[bool, typer.Option("--all", "-a", help="Show all services including stopped ones")] = False,
    detailed: Annotated[bool, typer.Option("--detailed", "-d", help="Show detailed information (multi-line format)")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show collection statistics (INFO level logging)")] = False,
    legend: Annotated[bool, typer.Option("--legend", help="Show legend explaining status symbols")] = False,
    config: Annotated[str | None, typer.Option("--config", "-c", help="Path to services config file (default: config/services.yaml)")] = None,
    host: Annotated[str | None, typer.Option("--host", help="NATS server host (overrides config)")] = None,
    port: Annotated[int | None, typer.Option("--port", help="NATS server port (overrides config)")] = None,
    subject_prefix: Annotated[str | None, typer.Option("--prefix", help="NATS subject prefix (overrides config)")] = None,
):
    """List TCS services with their current status.

    NATS connection settings are resolved from the config file (default
    `config/services.yaml`) with `.env` and `NATS_HOST`/`NATS_PORT` env-var
    fallbacks. CLI flags `--host`/`--port`/`--prefix` override resolved values.

    Shows running services by default. Use --all to include stopped services.
    Use SERVICE argument to filter by service name (shows service even if stopped).
    Use --verbose to show collection statistics and timing.
    """
    if legend:
        display_legend()
        return

    # Setup logging early so bootstrap helpers' output is visible
    if verbose:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    # Load .env (existing env vars take precedence) — same behaviour as launchers
    load_dotenv_if_available()

    # Resolve config file: explicit path must exist, default is best-effort
    config_file = determine_config_file(config, logger=logging.getLogger("tcsctl"))

    # Build a minimal ConfigurationManager (no ProcessContext — tcsctl is a
    # snapshot client, not a service process). FileConfigSource is no-op when
    # file is missing, so adding it unconditionally is safe.
    config_manager = ConfigurationManager()
    config_manager.add_source(FileConfigSource(config_file))

    # Resolve NATS settings: config -> env -> defaults; CLI flags override
    nats_settings = resolve_nats_settings(
        config_manager,
        host_override=host,
        port_override=port,
        subject_prefix_override=subject_prefix,
    )

    _run_list(
        nats_settings=nats_settings,
        all=all,
        detailed=detailed,
        service=service,
    )
