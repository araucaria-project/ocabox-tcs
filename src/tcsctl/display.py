"""Display formatting for TCS CLI using Rich library."""

from datetime import datetime, timezone
from rich.console import Console
from rich.text import Text

from tcsctl.client import ServiceInfo
from ocabox_tcs.monitoring import Status


# Status symbols (simple ASCII)
STATUS_SYMBOLS = {
    Status.OK: "●",
    Status.IDLE: "●",  # Healthy, no active work
    Status.BUSY: "●",  # Healthy, processing tasks
    Status.STARTUP: "○",
    Status.DEGRADED: "◐",
    Status.WARNING: "◐",
    Status.ERROR: "×",
    Status.FAILED: "×",
    Status.SHUTDOWN: "-",
    Status.UNKNOWN: "?",
}

# Status colors
STATUS_COLORS = {
    Status.OK: "green",
    Status.IDLE: "green",  # Healthy, no active work
    Status.BUSY: "green",  # Healthy, processing tasks
    Status.STARTUP: "cyan",
    Status.DEGRADED: "yellow",
    Status.WARNING: "yellow",
    Status.ERROR: "red",
    Status.FAILED: "red",
    Status.SHUTDOWN: "dim",
    Status.UNKNOWN: "dim",
}

# Heartbeat colors (no symbols needed, using ♥)
HEARTBEAT_COLORS = {
    "alive": "green",
    "stale": "yellow",
    "dead": "red",
}


def _format_service_name(service_id: str, show_full_with_dim: bool = False) -> Text:
    """Format service name with optional prefix handling.

    Args:
        service_id: Full service ID
        show_full_with_dim: If True (detailed mode), show full name with dimmed prefix.
                           If False (normal mode), omit common prefixes entirely.
    """
    text = Text()

    if show_full_with_dim:
        # Detailed mode - show full name with dimmed prefix
        prefixes = [
            "ocabox_tcs.services.examples.",
            "ocabox_tcs.services.",
        ]

        remaining = service_id
        for prefix in prefixes:
            if remaining.startswith(prefix):
                text.append(prefix, style="dim")
                remaining = remaining[len(prefix):]
                break

        text.append(remaining, style="bold")
    else:
        # Normal mode - omit common prefixes entirely
        prefixes = [
            "ocabox_tcs.services.examples.",
            "ocabox_tcs.services.",
        ]

        remaining = service_id
        for prefix in prefixes:
            if remaining.startswith(prefix):
                remaining = remaining[len(prefix):]
                break

        text.append(remaining, style="bold")

    return text


def display_legend():
    """Display legend explaining status symbols and colors."""
    console = Console()

    console.print()
    console.print("Legend:", style="bold")
    console.print()

    # Status symbols
    legend = Text()
    legend.append("  ● ", style="green")
    legend.append("green   = healthy (ok status + alive heartbeat)\n")

    legend.append("  ◐ ", style="yellow")
    legend.append("yellow  = warning (degraded status or stale heartbeat)\n")

    legend.append("  ⚠ ", style="red")
    legend.append("red     = problem (error/failed status or dead/missing heartbeat)\n")

    legend.append("  × ", style="red")
    legend.append("red     = error/failed status\n")

    legend.append("  ○ ", style="dim")
    legend.append("dim     = stopped service\n")

    console.print(legend)

    # Heartbeat symbols
    console.print("  Heartbeat indicators:")
    hb_legend = Text()
    hb_legend.append("    (♥) ", style="green")
    hb_legend.append("green  = alive (< 30s ago)\n")

    hb_legend.append("    (♥) ", style="yellow")
    hb_legend.append("yellow = stale (30s - 2m ago)\n")

    hb_legend.append("    (♥) ", style="red")
    hb_legend.append("red    = dead (> 2m ago or missing)\n")

    console.print(hb_legend)
    console.print()


def _format_timestamp(dt: datetime | None) -> tuple[str, str]:
    """Format timestamp as ISO string + relative time.

    Returns:
        Tuple of (iso_string, relative_string) e.g. ("2025-10-07 19:08:45 UTC", "10h 53m ago")
    """
    if dt is None:
        return ("N/A", "")

    # ISO format in UTC
    iso_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Relative time
    now = datetime.now(timezone.utc)
    delta = now - dt
    total_seconds = delta.total_seconds()

    if total_seconds < 60:
        rel_str = f"({int(total_seconds)}s ago)"
    elif total_seconds < 3600:
        rel_str = f"({int(total_seconds / 60)}m ago)"
    elif total_seconds < 86400:
        hours = int(total_seconds / 3600)
        mins = int((total_seconds % 3600) / 60)
        rel_str = f"({hours}h {mins}m ago)"
    else:
        days = int(total_seconds / 86400)
        hours = int((total_seconds % 86400) / 3600)
        rel_str = f"({days}d {hours}h ago)"

    return (iso_str, rel_str)


def display_services_detailed(services: list[ServiceInfo], show_all: bool = False, service_filter: str | None = None):
    """Display services in detailed multi-line format with hierarchical grouping.

    Services with a parent are grouped under their parent.

    Args:
        services: List of ServiceInfo objects
        show_all: If True, show all services including stopped ones (ignored if service_filter is set)
        service_filter: Optional service name filter (substring match, case-insensitive)
    """
    console = Console()

    # Apply service filter if provided (overrides show_all)
    if service_filter:
        filter_lower = service_filter.lower()
        services = [s for s in services if filter_lower in s.service_id.lower()]
    elif show_all:
        # --show_all: show declared + fresh (hide only: old ephemeral)
        services = [s for s in services if s.is_declared or s.is_fresh]
    else:
        # Default: show running + (fresh AND ERROR/FAILED)
        # But exclude old ephemeral even if "running" (likely zombie/stale)
        services = [
            s for s in services
            if (s.is_running and not (s.is_old and s.is_ephemeral))
            or (s.is_fresh and s.status in (Status.ERROR, Status.FAILED))
        ]

    if not services:
        if service_filter:
            console.print(f"No services matching '{service_filter}' found", style="dim")
        else:
            console.print("No services found", style="dim")
        return

    # Group services by parent for hierarchical display
    parent_to_children: dict[str | None, list[ServiceInfo]] = {}
    for service in services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    # Sort children within each group
    for children in parent_to_children.values():
        children.sort(key=lambda s: s.service_id)

    console.print()

    # Helper to print a single service in detailed format
    def print_service_detailed(service: ServiceInfo, show_separator: bool = True):
        # Status symbol and service name
        if not service.is_running:
            # Stopped services with ERROR/FAILED should show red × (not grey ○)
            if service.status in (Status.ERROR, Status.FAILED):
                status_symbol = "×"
                status_color = "red"
            else:
                status_symbol = "○"
                status_color = "dim"
        else:
            # Running services: heartbeat-first priority
            hb_status = service.heartbeat_status
            if hb_status == "dead":
                status_symbol = "⚠"
                status_color = "red"
            elif hb_status == "stale":
                status_symbol = "◐"
                status_color = "yellow"
            elif service.status in (Status.ERROR, Status.FAILED):
                status_symbol = "×"
                status_color = "red"
            elif service.status in (Status.DEGRADED, Status.WARNING):
                status_symbol = "◐"
                status_color = "yellow"
            else:
                status_symbol = "●"
                status_color = "green"

        # Main line: symbol + full service name (with dimmed prefix) + status + heartbeat
        main_line = Text()
        main_line.append(f"{status_symbol} ", style=status_color)
        # Detailed mode - show full name with dimmed prefix
        name_text = _format_service_name(service.service_id, show_full_with_dim=True)
        main_line.append(name_text)
        main_line.append(f" [{service.status.value}]", style=status_color)

        # Heartbeat indicator (only for alive/stale heartbeats)
        # Don't show heart for dead/missing heartbeats - status symbol already indicates problem
        if service.is_running:
            hb_status = service.heartbeat_status
            if hb_status == "alive":
                main_line.append(" (♥)", style="green")
            elif hb_status == "stale":
                main_line.append(" (♥)", style="yellow")
            # For "dead" and "none": don't show heart symbol at all

        console.print(main_line)

        # Detail lines (indented with 4 spaces)
        indent = "    "

        # Runner ID
        if service.runner_id:
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Runner:      ", style="dim")
            detail.append(service.runner_id, style="")
            console.print(detail)

        # Hostname
        if service.hostname:
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Host:        ", style="dim")
            detail.append(service.hostname, style="")
            console.print(detail)

        # PID
        if service.pid:
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("PID:         ", style="dim")
            detail.append(str(service.pid), style="")
            console.print(detail)

        # Start time
        if service.start_time:
            iso_str, rel_str = _format_timestamp(service.start_time)
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Started:     ", style="dim")
            detail.append(iso_str, style="cyan")
            if rel_str:
                detail.append(f" {rel_str}", style="dim")
            console.print(detail)

        # Stop time (if stopped)
        if service.stop_time:
            iso_str, rel_str = _format_timestamp(service.stop_time)
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Stopped:     ", style="dim")
            detail.append(iso_str, style="cyan")
            if rel_str:
                detail.append(f" {rel_str}", style="dim")
            console.print(detail)

        # Last heartbeat
        if service.last_heartbeat:
            iso_str, rel_str = _format_timestamp(service.last_heartbeat)
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Last HB:     ", style="dim")
            detail.append(iso_str, style="cyan")
            if rel_str:
                detail.append(f" {rel_str}", style="dim")
            console.print(detail)

        # Last status update
        if service.last_status_update:
            iso_str, rel_str = _format_timestamp(service.last_status_update)
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Last Status: ", style="dim")
            detail.append(iso_str, style="cyan")
            if rel_str:
                detail.append(f" {rel_str}", style="dim")
            console.print(detail)

        # Uptime (for running services)
        if service.is_running:
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Uptime:      ", style="dim")
            detail.append(service.uptime_str, style="cyan")
            console.print(detail)

        # Status message
        if service.status_message:
            detail = Text()
            detail.append(f"{indent}", style="")
            detail.append("Message:     ", style="dim")
            detail.append(service.status_message, style="")
            console.print(detail)

        # Add blank line after service
        if show_separator:
            console.print()

    # Display services hierarchically
    # First, display entities without parents (top-level)
    if None in parent_to_children:
        for service in parent_to_children[None]:
            print_service_detailed(service, show_separator=True)
            # If this service is a parent, display its children
            if service.service_id in parent_to_children:
                for child in parent_to_children[service.service_id]:
                    print_service_detailed(child, show_separator=True)

    # Then, display orphaned children (parent exists but not in current list)
    all_parent_ids = {s.service_id for s in services}
    for parent_name, children in parent_to_children.items():
        if parent_name is not None and parent_name not in all_parent_ids:
            # Parent not in list, show children without parent header
            for child in children:
                print_service_detailed(child, show_separator=True)

    # Summary
    console.print()
    running = sum(1 for s in services if s.is_running)
    total = len(services)

    summary = Text()
    summary.append(f"{running} loaded services", style="green" if running > 0 else "dim")
    if service_filter:
        summary.append(f" (filtered by '{service_filter}')", style="dim")
    elif not show_all and running < total:
        summary.append(f" ({total - running} stopped hidden, use --all)", style="dim")

    console.print(summary)
    console.print()


def display_services_table(services: list[ServiceInfo], show_all: bool = False, service_filter: str | None = None):
    """Display services in systemctl-like format with hierarchical grouping.

    Services with a parent are grouped and indented under their parent.

    Args:
        services: List of ServiceInfo objects
        show_all: If True, show all services including stopped ones (ignored if service_filter is set)
        service_filter: Optional service name filter (substring match, case-insensitive)
    """
    console = Console()

    # Apply service filter if provided (overrides show_all)
    if service_filter:
        filter_lower = service_filter.lower()
        services = [s for s in services if filter_lower in s.service_id.lower()]
    elif show_all:
        # --show_all: show declared + fresh (hide only: old ephemeral)
        services = [s for s in services if s.is_declared or s.is_fresh]
    else:
        # Default: show running + (fresh AND ERROR/FAILED)
        # But exclude old ephemeral even if "running" (likely zombie/stale)
        services = [
            s for s in services
            if (s.is_running and not (s.is_old and s.is_ephemeral))
            or (s.is_fresh and s.status in (Status.ERROR, Status.FAILED))
        ]

    if not services:
        if service_filter:
            console.print(f"No services matching '{service_filter}' found", style="dim")
        else:
            console.print("No services found", style="dim")
        return

    # Group services by parent for hierarchical display
    parent_to_children: dict[str | None, list[ServiceInfo]] = {}
    for service in services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    # Sort children within each group
    for children in parent_to_children.values():
        children.sort(key=lambda s: s.service_id)

    # Print header
    console.print()

    # Helper to print a single service
    def print_service(service: ServiceInfo, indent: str = ""):
        """Print a single service line with optional indentation."""
        if not service.is_running:
            # Stopped services with ERROR/FAILED should show red × (not grey ○)
            if service.status in (Status.ERROR, Status.FAILED):
                status_symbol = "×"
                status_color = "red"
            else:
                status_symbol = "○"
                status_color = "dim"
        else:
            # Running services: heartbeat-first priority
            hb_status = service.heartbeat_status

            if hb_status == "dead":
                # Zombie process or no heartbeat - CRITICAL
                status_symbol = "⚠"
                status_color = "red"
            elif hb_status == "stale":
                # Stale heartbeat - WARNING
                status_symbol = "◐"
                status_color = "yellow"
            elif service.status in (Status.ERROR, Status.FAILED):
                # Service reported error/failed
                status_symbol = "×"
                status_color = "red"
            elif service.status in (Status.DEGRADED, Status.WARNING):
                # Service reported degraded/warning
                status_symbol = "◐"
                status_color = "yellow"
            else:
                # Healthy: OK status + alive heartbeat
                status_symbol = "●"
                status_color = "green"

        # Build line
        line = Text()

        # Indentation for children
        line.append(indent)

        # Status symbol and name
        line.append(f"{status_symbol} ", style=status_color)
        line.append(_format_service_name(service.service_id))

        # Status text
        line.append(f" [{service.status.value}]", style=status_color)

        # Heartbeat indicator (only for alive/stale heartbeats)
        # Don't show heart for dead/missing heartbeats - status symbol already indicates problem
        if service.is_running:
            hb_status = service.heartbeat_status
            if hb_status == "alive":
                line.append(" (♥)", style="green")
            elif hb_status == "stale":
                line.append(" (♥)", style="yellow")
            # For "dead" and "none": don't show heart symbol at all

        # Uptime
        if service.is_running:
            line.append(f" up:{service.uptime_str}", style="cyan")

        # Message
        if service.status_message:
            # Truncate long messages
            msg = service.status_message
            if len(msg) > 60:
                msg = msg[:57] + "..."
            line.append(f" - {msg}", style="dim")

        console.print(line)

    # Display services hierarchically
    # First, display entities without parents (top-level)
    if None in parent_to_children:
        for service in parent_to_children[None]:
            print_service(service)
            # If this service is a parent, display its children indented
            if service.service_id in parent_to_children:
                for child in parent_to_children[service.service_id]:
                    print_service(child, indent="  ├─ ")

    # Then, display orphaned children (parent exists but not in current list)
    all_parent_ids = {s.service_id for s in services}
    for parent_name, children in parent_to_children.items():
        if parent_name is not None and parent_name not in all_parent_ids:
            # Parent not in list, show children without parent header
            for child in children:
                print_service(child)

    # Summary
    console.print()
    running = sum(1 for s in services if s.is_running)
    total = len(services)

    summary = Text()
    summary.append(f"{running} loaded services", style="green" if running > 0 else "dim")
    if service_filter:
        summary.append(f" (filtered by '{service_filter}')", style="dim")
    elif not show_all and running < total:
        summary.append(f" ({total - running} stopped hidden, use --all)", style="dim")

    console.print(summary)
    console.print()
