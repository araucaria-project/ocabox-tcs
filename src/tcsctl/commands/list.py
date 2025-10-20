"""List command for tcsctl."""

import asyncio
import logging
from typing import Annotated

import typer
from serverish.messenger import Messenger

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


def list_services(
    all: bool = False,
    detailed: bool = False,
    service: str | None = None,
    verbose: bool = False,
    host: str = "localhost",
    port: int = 4222,
    subject_prefix: str = "svc"
):
    """List TCS services with their current status.

    Shows running services by default. Use --all to include stopped services.
    """
    # Configure logging level based on verbose flag
    if verbose:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    # Collect data from NATS using ServiceControlClient
    try:
        asyncio.run(_list_services_async(
            all=all, detailed=detailed, service=service,
            host=host, port=port, subject_prefix=subject_prefix
        ))
    except TimeoutError:
        typer.secho(
            f"Timeout connecting to NATS at {host}:{port}. Is NATS server running?",
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
    host: Annotated[str, typer.Option("--host", help="NATS server host")] = "localhost",
    port: Annotated[int, typer.Option("--port", help="NATS server port")] = 4222,
    subject_prefix: Annotated[str, typer.Option("--prefix", help="NATS subject prefix for services")] = "svc"
):
    """List TCS services with their current status.

    Shows running services by default. Use --all to include stopped services.
    Use SERVICE argument to filter by service name (shows service even if stopped).
    Use --verbose to show collection statistics and timing.
    """
    if legend:
        display_legend()
        return

    list_services(all=all, detailed=detailed, service=service, verbose=verbose, host=host, port=port, subject_prefix=subject_prefix)
