"""Process-based launcher for running services as separate processes.

This launcher spawns each service in its own subprocess, suitable for
development and testing environments.
"""

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.management.process_context import ProcessContext


@dataclass
class ProcessInfo:
    """Information about a running service process."""
    process: subprocess.Popen
    start_time: datetime
    args: list[str]


class ProcessRunner(BaseRunner):
    """Runner that manages a service in a subprocess."""

    def __init__(self, config: ServiceRunnerConfig, terminate_delay: float = 1.0):
        super().__init__(config)
        self.process_info: ProcessInfo | None = None
        self.terminate_delay = terminate_delay
        self._log_monitor_task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Start service in subprocess."""
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            # Resolve module name: use explicit module if provided, else default to internal
            if self.config.module:
                module_name = self.config.module
            else:
                module_name = f"ocabox_tcs.services.{self.config.service_type}"

            args = [
                "poetry", "run", "python", "-m",
                module_name,
            ]

            if self.config.config_file:
                config_path = os.path.abspath(self.config.config_file)
                args.append(config_path)

            args.append(self.config.instance_context or "main")

            # Add runner_id if available
            if self.config.runner_id:
                args.extend(["--runner-id", self.config.runner_id])

            # Suppress banner in subprocesses (launcher already showed one)
            args.append("--no-banner")

            self.logger.info(f"Starting service: {' '.join(args)}")

            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            self.process_info = ProcessInfo(
                process=process,
                start_time=datetime.now(),
                args=args
            )

            self._is_running = True
            self._log_monitor_task = asyncio.create_task(self._monitor_logs())
            self.logger.info(f"Service {self.service_id} started (PID: {process.pid})")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start {self.service_id}: {e}", exc_info=True)
            self._is_running = False
            return False

    async def stop(self) -> bool:
        """Stop service subprocess."""
        if not self._is_running or not self.process_info:
            self.logger.warning(f"Service {self.service_id} not running")
            return False

        try:
            self.logger.info(f"Stopping {self.service_id}")
            proc = self.process_info.process

            proc.terminate()

            # Poll every 100ms for up to terminate_delay seconds
            # Exit early if process terminates cleanly
            force_killed = False
            poll_interval = 0.1
            max_polls = int(self.terminate_delay / poll_interval)

            for _ in range(max_polls):
                await asyncio.sleep(poll_interval)
                if proc.poll() is not None:
                    # Process exited cleanly
                    break
            else:
                # Timeout reached, check one more time and force kill if needed
                if proc.poll() is None:
                    self.logger.warning(
                        f"Force killing {self.service_id} - did not terminate in {self.terminate_delay}s"
                    )
                    proc.kill()
                    force_killed = True

            if self._log_monitor_task:
                self._log_monitor_task.cancel()
                try:
                    await self._log_monitor_task
                except asyncio.CancelledError:
                    pass

            # If we force-killed the service, publish STOP event to NATS
            # (subprocess didn't get a chance to send it)
            if force_killed:
                await self._publish_stop_event(reason="force_killed")

            self._is_running = False
            self.process_info = None
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
        if not self._is_running or not self.process_info:
            return {
                "service_id": self.service_id,
                "status": "stopped",
                "running": False
            }

        return {
            "service_id": self.service_id,
            "status": "running",
            "running": True,
            "pid": self.process_info.process.pid,
            "start_time": self.process_info.start_time.isoformat(),
            "uptime_seconds": (datetime.now() - self.process_info.start_time).total_seconds()
        }

    async def _publish_stop_event(self, reason: str = "force_killed"):
        """Publish STOP event to NATS registry.

        Called when the launcher force-kills a service, since the subprocess
        won't have a chance to send its own STOP message.

        Args:
            reason: Reason for stop (e.g., "force_killed", "timeout")
        """
        try:
            from serverish.messenger import single_publish
            from serverish.base import dt_utcnow_array

            # Get messenger from ProcessContext
            process_ctx = ProcessContext()
            if process_ctx is None or process_ctx.messenger is None:
                self.logger.warning("No NATS messenger available, cannot publish STOP event")
                return

            # Construct service_id in same format as service uses: module_name:instance_id
            # Service uses format like: ocabox_tcs.services.examples.01_minimal:minimal
            if self.config.module:
                module_name = self.config.module
            else:
                module_name = f"ocabox_tcs.services.{self.config.service_type}"

            instance_id = self.config.instance_context or self.config.service_type
            service_id = f"{module_name}:{instance_id}"

            subject = f"svc.registry.stop.{service_id}"
            data = {
                "event": "stop",
                "service_id": service_id,
                "timestamp": dt_utcnow_array(),
                "reason": reason,
                "killed_by_launcher": True
            }

            await single_publish(subject, data)
            self.logger.info(f"Published STOP event for {service_id} (reason: {reason})")

        except Exception as e:
            self.logger.error(f"Failed to publish STOP event for {self.service_id}: {e}", exc_info=True)

    async def _monitor_logs(self):
        """Monitor and relay service logs."""
        if not self.process_info:
            return

        try:
            while self._is_running and self.process_info:
                line = await asyncio.to_thread(self.process_info.process.stderr.readline)
                if not line:
                    break
                self.logger.info(f"[{self.service_id}] {line.strip()}")
        except Exception as e:
            self.logger.error(f"Log monitoring error for {self.service_id}: {e}")


class ProcessLauncher(BaseLauncher):
    """Launcher that manages services as separate processes."""

    def __init__(self, launcher_id: str | None = None, terminate_delay: float = 1.0):
        # Generate unique launcher ID: launcher-type.hostname.random-suffix
        if launcher_id is None:
            import socket
            from serverish.base.idmanger import gen_uid
            hostname_short = socket.gethostname().split('.')[0]
            unique_suffix = gen_uid("process-launcher").split("process-launcher", 1)[1]
            launcher_id = f"process-launcher.{hostname_short}{unique_suffix}"

        super().__init__(launcher_id)
        self.terminate_delay = terminate_delay
        self._shutdown_event = asyncio.Event()
        self.process_ctx: ProcessContext | None = None

    async def initialize(self, process_ctx: ProcessContext) -> bool:
        """Initialize launcher from ProcessContext.

        Uses already-initialized ProcessContext to get services configuration
        and registers runners for spawning services.

        Args:
            process_ctx: Already-initialized ProcessContext

        Returns:
            True if initialization successful
        """
        try:
            # Store ProcessContext reference
            self.process_ctx = process_ctx
            self.logger.info("Using ProcessContext for process launcher")

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
                    config_file=process_ctx.config_file,  # Use stored config file path
                    runner_id=f"{self.launcher_id}.{service_cfg['type']}"
                )

                runner = ProcessRunner(runner_config, terminate_delay=self.terminate_delay)
                self.runners[runner.service_id] = runner
                self.logger.info(f"Registered runner for {runner.service_id}")

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
        """Stop all running services in parallel."""
        if not self.runners:
            return True

        # Stop all services in parallel for faster shutdown
        results = await asyncio.gather(
            *[self.stop_service(sid) for sid in self.runners.keys()],
            return_exceptions=True
        )

        # Check if any failed
        success = True
        for sid, result in zip(self.runners.keys(), results):
            if isinstance(result, Exception):
                self.logger.error(f"Failed to stop {sid}: {result}")
                success = False
            elif not result:
                self.logger.error(f"Failed to stop {sid}")
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

        self.logger.info("Services started. Press Ctrl+C to stop.")
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
    """Process launcher entry point."""
    import argparse
    import logging
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-15s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger = logging.getLogger("launch")

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Start TCS process launcher")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to services config file (default: config/services.yaml)"
    )
    parser.add_argument(
        "--terminate-delay",
        type=float,
        default=1.0,
        help="Time to wait for graceful shutdown before force-kill (default: 1.0s)"
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
        logger.info("Launcher: Process (each service in separate subprocess)")
        logger.info("=" * 60)

    # Initialize ProcessContext (handles config loading)
    process_ctx = await ProcessContext.initialize(config_file=config_file)

    # Create and initialize launcher
    launcher = ProcessLauncher(terminate_delay=args.terminate_delay)
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
    """Entry point for process launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()