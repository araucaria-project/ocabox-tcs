"""Main Typer app for tcsctl CLI.

This module defines the main CLI application using Typer.
The app is exported for potential future integration with the oca CLI.

Usage:
    Standalone: tcsctl list
    Future integration: oca svc list (via app object)
"""

import os
import typer
from tcsctl.commands.list import list_services_cmd

# Disable typer's rich integration to avoid compatibility issues
os.environ["_TYPER_STANDARD_TRACEBACK"] = "1"

# For now, make list the direct command (no subcommands)
# In future, when we add more commands (monitor, etc.), convert back to Typer app
app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)
app.command()(list_services_cmd)


def main():
    """Entry point for tcsctl CLI."""
    app()


if __name__ == "__main__":
    main()
