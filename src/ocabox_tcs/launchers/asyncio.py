"""Asyncio-based launcher for running services in same process.

This launcher runs all services within the same Python process using asyncio,
suitable for development and resource-constrained environments.
"""

import asyncio
import logging
import signal
from datetime import datetime
from typing import Any

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.management.process_context import ProcessContext
from ocabox_tcs.management.service_controller import ServiceController


class AsyncioRunner(BaseRunner):
    """Runner that manages a service within the same process using asyncio."""

    def __init__(self, config: ServiceRunnerConfig):
        super().__init__(config)
        self.controller: ServiceController | None = None
        self.start_time: datetime | None = None

    async def start(self) -> bool:
        """Start service in current process.

        Note: ProcessContext must already be initialized by AsyncioLauncher.
        """
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            # Resolve module name: use explicit module if provided, else default to internal
            if self.config.module:
                module_name = self.config.module
            else:
                module_name = f"ocabox_tcs.services.{self.config.service_type}"
            instance_id = self.config.instance_context or self.config.service_type

            self.controller = ServiceController(
                module_name=module_name,
                instance_id=instance_id,
                runner_id=self.config.runner_id
            )

            # ProcessContext already initialized - just initialize controller
            if not await self.controller.initialize():
                self.logger.error(f"Failed to initialize {self.service_id}")
                return False

            if not await self.controller.start_service():
                self.logger.error(f"Failed to start {self.service_id}")
                return False

            self._is_running = True
            self.start_time = datetime.now()
            self.logger.info(f"Service {self.service_id} started in-process")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start {self.service_id}: {e}", exc_info=True)
            self._is_running = False
            return False

    async def stop(self) -> bool:
        """Stop service."""
        if not self._is_running or not self.controller:
            self.logger.warning(f"Service {self.service_id} not running")
            return False

        try:
            self.logger.info(f"Stopping {self.service_id}")
            await self.controller.stop_service()
            await self.controller.shutdown()

            self._is_running = False
            self.controller = None
            self.start_time = None
            self.logger.info(f"Service {self.service_id} stopped")
            return True

        except Exception as e:
            self.logger.error(f"Failed to stop {self.service_id}: {e}")
            return False

    async def restart(self) -> bool:
        """Restart the service."""
        if await self.stop():
            await asyncio.sleep(0.5)
            return await self.start()
        return False

    async def get_status(self) -> dict[str, Any]:
        """Get service status."""
        if not self._is_running or not self.controller or not self.start_time:
            return {
                "service_id": self.service_id,
                "status": "stopped",
                "running": False
            }

        return {
            "service_id": self.service_id,
            "status": "running",
            "running": self.controller.is_running,
            "start_time": self.start_time.isoformat(),
            "uptime_seconds": (datetime.now() - self.start_time).total_seconds()
        }


class AsyncioLauncher(BaseLauncher):
    """Launcher that manages services within the same process using asyncio."""

    def __init__(self, launcher_id: str | None = None):
        # Generate unique launcher ID: launcher-type.hostname.random-suffix
        if launcher_id is None:
            import socket
            from serverish.base.idmanger import gen_uid
            hostname_short = socket.gethostname().split('.')[0]
            unique_suffix = gen_uid("asyncio-launcher").split("asyncio-launcher", 1)[1]
            launcher_id = f"asyncio-launcher.{hostname_short}{unique_suffix}"

        super().__init__(launcher_id)
        self._shutdown_event = asyncio.Event()
        self.process_ctx: ProcessContext | None = None

    async def initialize(self, process_ctx: ProcessContext) -> bool:
        """Initialize launcher from ProcessContext.

        Uses already-initialized ProcessContext (shared by all services in this process).

        Args:
            process_ctx: Already-initialized ProcessContext

        Returns:
            True if initialization successful
        """
        try:
            # Store ProcessContext reference (shared by all services)
            self.process_ctx = process_ctx
            self.logger.info("Using ProcessContext for asyncio launcher")

            # Initialize launcher monitoring (auto-detects NATS via ProcessContext)
            await self.initialize_monitoring(subject_prefix="svc")

            # Get services list from config_manager (use raw config to include 'services' key)
            raw_config = process_ctx.config_manager.get_raw_config()
            services_list = raw_config.get('services', [])

            if not services_list:
                self.logger.warning("No services found in configuration")
                return True

            # Register runners for each service
            for service_cfg in services_list:
                runner_config = ServiceRunnerConfig(
                    service_type=service_cfg['type'],
                    instance_context=service_cfg.get('instance_context'),
                    config_file=process_ctx.config_file,  # Not used by AsyncioRunner, but keep for consistency
                    runner_id=f"{self.launcher_id}.{service_cfg['type']}",
                    module=service_cfg.get('module')  # Optional: external package module
                )

                runner = AsyncioRunner(runner_config)
                self.runners[runner.service_id] = runner
                self.logger.info(f"Registered runner for {runner.service_id}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize launcher: {e}", exc_info=True)
            return False

    async def start_all(self) -> bool:
        """Start all configured services."""
        success = True
        for service_id, runner in self.runners.items():
            if not await runner.start():
                self.logger.error(f"Failed to start {service_id}")
                success = False

        # Start launcher monitoring after services are started
        if success:
            await self.start_monitoring()

        return success

    async def stop_all(self) -> bool:
        """Stop all running services."""
        success = True
        for service_id in list(self.runners.keys()):
            if not await self.stop_service(service_id):
                self.logger.error(f"Failed to stop {service_id}")
                success = False
        return success

    async def run(self):
        """Run launcher with signal handling."""
        loop = asyncio.get_running_loop()

        def handle_signal(sig):
            self.logger.info(f"Received signal {sig}, shutting down...")
            asyncio.create_task(self._shutdown())

        loop.add_signal_handler(signal.SIGINT, lambda: handle_signal("SIGINT"))
        loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal("SIGTERM"))

        self.logger.info("Services started (asyncio). Press Ctrl+C to stop.")
        await self._shutdown_event.wait()
        self.logger.info("Launcher shutdown complete")

    async def _shutdown(self):
        """Shutdown all services and process context."""
        # Stop launcher monitoring first
        await self.stop_monitoring()

        self.logger.info("Stopping all services...")
        await self.stop_all()

        if self.process_ctx:
            await self.process_ctx.shutdown()

        self._shutdown_event.set()


async def amain():
    """Asyncio launcher entry point."""
    import argparse
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-15s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger = logging.getLogger("launch")

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Start TCS asyncio launcher")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to services config file (default: config/services.yaml)"
    )
    parser.add_argument("--no-banner", action="store_true", help="Suppress startup banner")
    args = parser.parse_args()

    # Determine config file and validate
    if args.config is not None:
        # User explicitly provided --config, file MUST exist
        config_file = args.config
        if not Path(config_file).exists():
            logger.error(f"Configuration file not found: {config_file}")
            logger.error("Explicitly provided config file must exist. Exiting.")
            sys.exit(1)
    else:
        # Use default, missing file is OK (will use defaults)
        config_file = "config/services.yaml"
        if not Path(config_file).exists():
            logger.info(f"Default config file not found: {config_file}")
            logger.info("Continuing with empty configuration")

    # Print startup banner (unless suppressed)
    if not args.no_banner:
        logger.info("=" * 60)
        logger.info("TCS - Telescope Control Services")
        logger.info("Launcher: Asyncio (all services in same process)")
        logger.info("=" * 60)

    # Initialize ProcessContext (handles config loading, shared by all services)
    process_ctx = await ProcessContext.initialize(config_file=config_file)

    # Create and initialize launcher
    launcher = AsyncioLauncher()
    if not await launcher.initialize(process_ctx):
        logging.error("Failed to initialize launcher")
        await process_ctx.shutdown()
        return

    # Start services and run
    if not await launcher.start_all():
        logging.error("Failed to start services")
        await launcher.stop_all()
        await process_ctx.shutdown()
        return

    await launcher.run()


def main():
    """Entry point for asyncio launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()