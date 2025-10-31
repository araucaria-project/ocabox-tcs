"""Asyncio-based launcher for running services in same process.

This launcher runs all services within the same Python process using asyncio,
suitable for development and resource-constrained environments.
"""

import asyncio
import logging
import signal
from datetime import datetime
from time import time
from typing import Any

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.management.process_context import ProcessContext
from ocabox_tcs.management.service_controller import ServiceController


class AsyncioRunner(BaseRunner):
    """Runner that manages a service within the same process using asyncio."""

    def __init__(
        self,
        config: ServiceRunnerConfig,
        launcher_id: str | None = None,
        subject_prefix: str = "svc"
    ):
        super().__init__(config, launcher_id=launcher_id, subject_prefix=subject_prefix)
        self.controller: ServiceController | None = None
        self.start_time: datetime | None = None
        self._crash_monitor_task: asyncio.Task | None = None

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
            self._crash_monitor_task = asyncio.create_task(self._monitor_crash())

            # Publish START event to registry
            await self._publish_start_event()

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

            if self._crash_monitor_task:
                self._crash_monitor_task.cancel()
                try:
                    await self._crash_monitor_task
                except asyncio.CancelledError:
                    pass

            self._is_running = False
            self.controller = None
            self.start_time = None

            # Publish STOP event to registry
            await self._publish_stop_event()

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

    async def _monitor_crash(self):
        """Monitor service for unexpected completion and handle restarts."""
        if not self.controller:
            return

        try:
            while self._is_running and self.controller is not None:
                # Check if service is still running
                if not self.controller.is_running and self.controller is not None:
                    # Service crashed or stopped unexpectedly
                    # Clear controller immediately to prevent duplicate handling
                    controller = self.controller
                    self.controller = None

                    self.logger.warning(
                        f"Service {self.service_id} stopped unexpectedly"
                    )

                    # Determine if we should restart
                    should_restart = self._should_restart(exit_code=1)

                    if should_restart:
                        # Publish CRASH event
                        await self._publish_crash_event(exit_code=1)

                        # Wait restart delay
                        await asyncio.sleep(self.config.restart_sec)

                        # Publish RESTARTING event
                        await self._publish_restarting_event(attempt=len(self._restart_history) + 1)

                        # Attempt restart
                        self.logger.info(
                            f"Restarting {self.service_id} "
                            f"(attempt {len(self._restart_history) + 1})"
                        )

                        # Mark as not running (will be set to True by start())
                        self._is_running = False
                        self.start_time = None

                        # Restart
                        success = await self.start()

                        if success:
                            self._restart_history.append(time())
                            self._cleanup_restart_history()
                        else:
                            self.logger.error(
                                f"Failed to restart {self.service_id}, giving up"
                            )
                            await self._publish_failed_event(
                                reason="restart_failed"
                            )
                            break
                    else:
                        # No restart policy
                        self.logger.info(
                            f"Service {self.service_id} stopped (no restart policy)"
                        )
                        self._is_running = False
                        break

                # Check every second
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            # Normal stop via stop() method
            pass
        except Exception as e:
            self.logger.error(f"Crash monitor error for {self.service_id}: {e}")

    async def _publish_start_event(self):
        """Publish START event to NATS registry."""
        import socket
        import os
        await self._publish_registry_event(
            "start",
            status="startup",
            pid=os.getpid(),
            hostname=socket.gethostname()
        )

    async def _publish_stop_event(self, reason: str = "completed", exit_code: int = 0):
        """Publish STOP event to NATS registry.

        Args:
            reason: Reason for stop (default: "completed")
            exit_code: Exit code (default: 0)
        """
        await self._publish_registry_event(
            "stop",
            status="shutdown",
            reason=reason,
            exit_code=exit_code
        )

    async def _publish_crash_event(self, exit_code: int):
        """Publish CRASH event to NATS registry.

        Args:
            exit_code: Exit code (1 for asyncio crash)
        """
        will_restart = self._should_restart(exit_code)
        await self._publish_registry_event(
            "crashed",
            status="error" if will_restart else "failed",
            exit_code=exit_code,
            restart_policy=self.config.restart,
            will_restart=will_restart
        )

    async def _publish_restarting_event(self, attempt: int):
        """Publish RESTARTING event to NATS registry.

        Args:
            attempt: Restart attempt number (1-based)
        """
        await self._publish_registry_event(
            "restarting",
            status="startup",
            restart_attempt=attempt,
            max_restarts=self.config.restart_max if self.config.restart_max > 0 else None
        )

    async def _publish_failed_event(self, reason: str):
        """Publish FAILED event to NATS registry.

        Args:
            reason: Reason for failure
        """
        await self._publish_registry_event(
            "failed",
            status="failed",
            reason=reason,
            restart_count=len(self._restart_history)
        )


class AsyncioLauncher(BaseLauncher):
    """Launcher that manages services within the same process using asyncio."""

    def __init__(self, launcher_id: str | None = None):
        # Use provided launcher_id or default to simple name
        if launcher_id is None:
            launcher_id = "asyncio-launcher"

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
            self.logger.debug("Using ProcessContext for asyncio launcher")

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
                    module=service_cfg.get('module'),  # External service package (optional)
                    restart=service_cfg.get('restart', 'no'),  # Restart policy
                    restart_sec=float(service_cfg.get('restart_sec', 5.0)),  # Restart delay (seconds)
                    restart_max=int(service_cfg.get('restart_max', 0)),  # Max restarts (0=unlimited)
                    restart_window=float(service_cfg.get('restart_window', 60.0))  # Time window (seconds)
                )

                runner = AsyncioRunner(
                    runner_config,
                    launcher_id=self.launcher_id,
                    subject_prefix="svc"  # TODO: get from config like ProcessLauncher does
                )
                self.runners[runner.service_id] = runner
                self.logger.debug(f"Registered runner for {runner.service_id}")
                self.logger.debug(
                    f"Restart policy for {runner.service_id}: {runner_config.restart} "
                    f"(max={runner_config.restart_max}, delay={runner_config.restart_sec}s)"
                )

            # Declare services to registry (marks them as part of configuration)
            await self.declare_services(subject_prefix="svc")

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
    import os
    import socket
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

    # Generate deterministic launcher ID from config file path, pwd, and hostname
    launcher_id = BaseLauncher.gen_launcher_name(
        "asyncio-launcher",
        config_file,
        os.getcwd(),
        socket.gethostname()
    )

    # Create and initialize launcher
    launcher = AsyncioLauncher(launcher_id=launcher_id)
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