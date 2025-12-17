"""Unified TCS daemon launcher.

Provides single entry point with ability to choose execution method:
- Asyncio (default): All services in same process - simpler, lower resource usage
- Process: Each service in separate subprocess - better isolation

Usage:
    # Default (asyncio launcher)
    poetry run tcsd --config config/services.yaml

    # Process launcher
    poetry run tcsd --launcher process --config config/services.yaml

    # Disable colored logging
    poetry run tcsd --no-color

    # Process with custom terminate delay
    poetry run tcsd --launcher process --terminate-delay 2.0

Future: Launcher choice will be configurable via config file.
"""

import asyncio
import argparse


async def amain():
    """Unified launcher entry point."""
    from ocabox_tcs.launchers.process import ProcessLauncher
    from ocabox_tcs.launchers.asyncio import AsyncioLauncher

    # Pre-parse to detect launcher choice (before full argument parsing)
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--launcher",
        choices=["asyncio", "process"],
        default="asyncio",
        help="Launcher type: asyncio (default) or process"
    )
    pre_args, remaining = pre_parser.parse_known_args()

    # Choose launcher class based on --launcher flag
    if pre_args.launcher == "process":
        launcher_cls = ProcessLauncher
    else:
        launcher_cls = AsyncioLauncher

    # Restore sys.argv for full parsing by chosen launcher
    import sys
    sys.argv = [sys.argv[0]] + remaining

    # Create factory function for chosen launcher type
    def factory(launcher_id, args):
        if pre_args.launcher == "process":
            terminate_delay = getattr(args, 'terminate_delay', 1.0)
            return ProcessLauncher(launcher_id=launcher_id, terminate_delay=terminate_delay)
        else:
            return AsyncioLauncher(launcher_id=launcher_id)

    # Run via common_main template method
    await launcher_cls.common_main(factory)


def main():
    """Entry point for unified TCS daemon."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
