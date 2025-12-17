"""Process-based launcher for running services as separate processes.

This launcher spawns each service in its own subprocess, suitable for
development and testing environments.
"""

import asyncio
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from time import time
from typing import Any

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.management.process_context import ProcessContext
from ocabox_tcs.management.service_registry import ServiceRegistry


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
        registry: ServiceRegistry,
        launcher_id: str | None = None,
        subject_prefix: str = "svc",
        terminate_delay: float = 1.0
    ):
        super().__init__(config, launcher_id=launcher_id, subject_prefix=subject_prefix)
        self.registry = registry
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
            # Resolve module path via ServiceRegistry
            module_path = self.registry.resolve_module(self.config.service_type)

            args = [
                "python", "-m",
                module_path,
            ]

            if self.config.config_file:
                config_path = os.path.abspath(self.config.config_file)
                args.append(config_path)

            # Pass variant (was instance_context)
            args.append(self.config.variant)

            # Add runner_id if available
            if self.config.runner_id:
                args.extend(["--runner-id", self.config.runner_id])

            # Add parent_name for hierarchical display
            if self.config.parent_name:
                args.extend(["--parent-name", self.config.parent_name])

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

    @classmethod
    def create_argument_parser(cls, description: str | None = None):
        """Create parser with process-specific options."""
        if description is None:
            description = "Start TCS process launcher"
        parser = super().create_argument_parser(description)
        parser.add_argument(
            "--terminate-delay",
            type=float,
            default=1.0,
            help="Time to wait for graceful shutdown before force-kill (default: 1.0s)"
        )
        return parser

    def _get_launcher_type_display(self) -> str:
        """Get display name for banner."""
        return "Process (each service in separate subprocess)"

    def _create_runner(
        self,
        config: ServiceRunnerConfig,
        registry: ServiceRegistry,
        subject_prefix: str
    ) -> ProcessRunner:
        """Create ProcessRunner for subprocess execution."""
        return ProcessRunner(
            config,
            registry=registry,
            launcher_id=self.launcher_id,
            subject_prefix=subject_prefix,
            terminate_delay=self.terminate_delay
        )


async def amain():
    """Process launcher entry point."""
    def factory(launcher_id, args):
        return ProcessLauncher(launcher_id=launcher_id, terminate_delay=args.terminate_delay)

    await ProcessLauncher.common_main(factory)


def main():
    """Entry point for process launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
