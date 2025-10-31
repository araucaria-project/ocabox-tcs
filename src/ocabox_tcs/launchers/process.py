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
from time import time
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

    def __init__(
        self,
        config: ServiceRunnerConfig,
        launcher_id: str | None = None,
        subject_prefix: str = "svc",
        terminate_delay: float = 1.0
    ):
        super().__init__(config, launcher_id=launcher_id, subject_prefix=subject_prefix)
        self.process_info: ProcessInfo | None = None
        self.terminate_delay = terminate_delay
        self._log_monitor_task: asyncio.Task | None = None
        self._crash_monitor_task: asyncio.Task | None = None

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
            self._crash_monitor_task = asyncio.create_task(self._monitor_crash())
            self.logger.info(f"Service {self.service_id} started (PID: {process.pid})")

            # Publish START event (runner owns lifecycle events)
            await self._publish_start_event(pid=process.pid)

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

            if self._crash_monitor_task:
                self._crash_monitor_task.cancel()
                try:
                    await self._crash_monitor_task
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

    async def _publish_start_event(self, pid: int):
        """Publish START event to NATS registry.

        Called when subprocess starts successfully. Runner owns lifecycle events.

        Args:
            pid: Process ID of started subprocess
        """
        import socket
        await self._publish_registry_event(
            "start",
            status="startup",
            pid=pid,
            hostname=socket.gethostname()
        )

    async def _publish_stop_event(self, reason: str = "force_killed", exit_code: int = 0):
        """Publish STOP event to NATS registry.

        Called when service stops cleanly or is force-killed by launcher.

        Args:
            reason: Reason for stop (e.g., "completed", "force_killed")
            exit_code: Process exit code
        """
        await self._publish_registry_event(
            "stop",
            status="shutdown",
            reason=reason,
            exit_code=exit_code
        )

    async def _monitor_crash(self):
        """Monitor subprocess for unexpected exits and handle restarts."""
        if not self.process_info:
            return

        try:
            # Block until process exits - immediate detection!
            # This runs BEFORE Python cleanup triggers ServiceController.shutdown()
            returncode = await asyncio.to_thread(self.process_info.process.wait)

            # Process exited! Clear process_info immediately to prevent duplicate handling
            if self.process_info is not None:
                process_info = self.process_info
                self.process_info = None

                # Check if it's a clean exit (exit code 0)
                if returncode == 0:
                    self.logger.info(f"Service {self.service_id} exited cleanly (exit code: 0)")
                    # Publish STOP event for clean exit
                    await self._publish_stop_event(reason="completed", exit_code=returncode)
                    self._is_running = False
                    return

                self.logger.warning(
                    f"Service {self.service_id} exited unexpectedly "
                    f"(exit code: {returncode})"
                )

                # Determine if we should restart
                should_restart = self._should_restart(returncode)

                if should_restart:
                    # Check restart limits
                    if self.config.restart_max > 0:
                        self._cleanup_restart_history()
                        if len(self._restart_history) >= self.config.restart_max:
                            self.logger.error(
                                f"Service {self.service_id} reached restart limit "
                                f"({self.config.restart_max} restarts in {self.config.restart_window}s), giving up"
                            )
                            await self._publish_crash_event(exit_code=returncode)
                            await self._publish_failed_event(reason="restart_limit_reached")
                            self._is_running = False
                            return

                    # Publish CRASH event
                    await self._publish_crash_event(exit_code=returncode)

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
                else:
                    # No restart policy - publish crash event with failed status
                    self.logger.info(
                        f"Service {self.service_id} crashed (no restart policy)"
                    )
                    await self._publish_crash_event(exit_code=returncode)
                    self._is_running = False

        except asyncio.CancelledError:
            # Normal stop via stop() method
            pass
        except Exception as e:
            self.logger.error(f"Crash monitor error for {self.service_id}: {e}")

    async def _publish_crash_event(self, exit_code: int):
        """Publish CRASH event to NATS registry.

        Args:
            exit_code: Process exit code
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
            reason: Reason for failure (e.g., 'restart_failed', 'restart_limit_reached')
        """
        await self._publish_registry_event(
            "failed",
            status="failed",
            reason=reason,
            restart_count=len(self._restart_history)
        )

    async def publish_declared(self):
        """Publish DECLARED event to NATS registry.

        Called by launcher after runner creation. Only publishes if runner_id
        is present (skips for ad-hoc standalone/test runs).

        This marks the service as part of the launcher's formal configuration,
        distinguishing it from ephemeral services.
        """
        await self._publish_registry_event(
            "declared",
            restart_policy=self.config.restart,
            # Note: parent and runner_id are added automatically by _publish_registry_event
        )

    async def _monitor_logs(self):
        """Monitor and relay service logs."""
        if not self.process_info:
            return

        try:
            while self._is_running and self.process_info:
                line = await asyncio.to_thread(self.process_info.process.stderr.readline)
                if not line:
                    break
                # Logger name already includes service_id (run|{service_id}), no need to repeat
                self.logger.info(line.strip())
        except Exception as e:
            self.logger.error(f"Log monitoring error for {self.service_id}: {e}")


class ProcessLauncher(BaseLauncher):
    """Launcher that manages services as separate processes."""

    def __init__(self, launcher_id: str | None = None, terminate_delay: float = 1.0):
        # Use provided launcher_id or default to simple name
        if launcher_id is None:
            launcher_id = "process-launcher"

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
            self.logger.debug("Using ProcessContext for process launcher")

            # Get subject_prefix from NATS config
            subject_prefix = 'svc'  # Default
            if process_ctx.config_manager:
                global_config = process_ctx.config_manager.resolve_config()
                nats_config = global_config.get("nats", {})
                subject_prefix = nats_config.get("subject_prefix", "svc")
                self.logger.debug(f"Using NATS subject prefix: {subject_prefix}")

            # Store subject_prefix for runners to use
            self.subject_prefix = subject_prefix

            # Initialize launcher monitoring (auto-detects NATS via ProcessContext)
            await self.initialize_monitoring(subject_prefix=subject_prefix)

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
                    runner_id=f"{self.launcher_id}.{service_cfg['type']}",
                    module=service_cfg.get('module'),  # External service package (optional)
                    restart=service_cfg.get('restart', 'no'),  # Restart policy
                    restart_sec=float(service_cfg.get('restart_sec', 5.0)),  # Restart delay (seconds)
                    restart_max=int(service_cfg.get('restart_max', 0)),  # Max restarts (0=unlimited)
                    restart_window=float(service_cfg.get('restart_window', 60.0))  # Time window (seconds)
                )

                runner = ProcessRunner(
                    runner_config,
                    launcher_id=self.launcher_id,
                    subject_prefix=subject_prefix,
                    terminate_delay=self.terminate_delay
                )
                self.runners[runner.service_id] = runner
                self.logger.debug(f"Registered runner for {runner.service_id}")
                self.logger.debug(
                    f"Restart policy for {runner.service_id}: {runner_config.restart} "
                    f"(max={runner_config.restart_max}, delay={runner_config.restart_sec}s)"
                )

            # Declare services to registry (marks them as part of configuration)
            await self.declare_services(subject_prefix=subject_prefix)

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

    # Generate deterministic launcher ID from config file path, pwd, and hostname
    launcher_id = BaseLauncher.gen_launcher_name(
        "process-launcher",
        config_file,
        os.getcwd(),
        socket.gethostname()
    )

    # Create and initialize launcher
    launcher = ProcessLauncher(launcher_id=launcher_id, terminate_delay=args.terminate_delay)
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
