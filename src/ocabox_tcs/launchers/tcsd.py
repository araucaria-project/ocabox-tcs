"""Unified TCS daemon launcher.

Provides single entry point with ability to choose execution method:
- Asyncio (default): All services in same process - simpler, lower resource usage
- Process: Each service in separate subprocess - better isolation

Usage:
    # Default (asyncio launcher)
    tcsd --config config/services.yaml

    # Process launcher
    tcsd --launcher process --config config/services.yaml

    # Disable colored logging
    tcsd --no-color

    # Process with custom terminate delay
    tcsd --launcher process --terminate-delay 2.0

Future: Launcher choice will be configurable via config file.
"""

import asyncio


async def amain():
    """Unified launcher entry point."""
    import argparse
    import os
    import socket
    from ocabox_tcs.launchers.base_launcher import BaseLauncher
    from ocabox_tcs.launchers.process import ProcessLauncher
    from ocabox_tcs.launchers.asyncio import AsyncioLauncher

    def customize_parser(base_parser):
        """Customize parser for unified launcher."""
        parser = argparse.ArgumentParser(
            description="Start TCS unified launcher (tcsd)",
            parents=[base_parser],
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Launcher Types:
  asyncio  All services run in the same process (default)
           - Lower resource usage, simpler debugging
           - Services share memory space

  process  Each service runs in a separate subprocess
           - Better isolation, services can't interfere
           - Higher resource usage
           - Additional option: --terminate-delay

Examples:
  tcsd --config config/services.yaml                    # Default asyncio mode
  tcsd --launcher process --config config/services.yaml # Process mode
  tcsd --no-color                                       # Plain text logging
        """
        )

        # Add launcher choice
        parser.add_argument(
            "-l",
            "--launcher",
            choices=["asyncio", "process"],
            default="asyncio",
            help="Launcher type (default: asyncio)"
        )

        # Add process-specific option
        parser.add_argument(
            "--terminate-delay",
            type=float,
            default=1.0,
            help="[process only] Time to wait for graceful shutdown (default: 1.0s)"
        )

        return parser

    def factory(launcher_id, args):
        """Create launcher based on --launcher choice."""
        # Generate proper launcher ID
        config_file = BaseLauncher.determine_config_file(args.config)

        if args.launcher == "process":
            launcher_id = BaseLauncher.gen_launcher_name(
                "process-launcher",
                config_file,
                os.getcwd(),
                socket.gethostname()
            )
            return ProcessLauncher(launcher_id=launcher_id, terminate_delay=args.terminate_delay)
        else:
            launcher_id = BaseLauncher.gen_launcher_name(
                "asyncio-launcher",
                config_file,
                os.getcwd(),
                socket.gethostname()
            )
            return AsyncioLauncher(launcher_id=launcher_id)

    await BaseLauncher.launch(factory, customize_parser)


def main():
    """Entry point for unified TCS daemon."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
