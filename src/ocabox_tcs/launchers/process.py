"""Process-based launcher for running services as separate processes.

This launcher spawns each service in its own subprocess, suitable for
development and testing environments.
"""

import asyncio
import os
import sys
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
    process: asyncio.subprocess.Process
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
        terminate_delay: float = 5.0
    ):
        super().__init__(config, launcher_id=launcher_id, subject_prefix=subject_prefix)
        self.registry = registry
        self.process_info: ProcessInfo | None = None
        self.terminate_delay = terminate_delay
        self._drain_tasks: list[asyncio.Task] = []
        self._crash_monitor_task: asyncio.Task | None = None
        self._stopping_gracefully: bool = False  # Track if we initiated stop

    async def start(self) -> bool:
        """Start service in subprocess."""
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            # Resolve module path via ServiceRegistry
            module_path = self.registry.resolve_module(self.config.service_type)

            args = [
                sys.executable, "-m",
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

            # Use asyncio-native subprocess so log draining is handled on the
            # event loop instead of the default thread pool. The previous
            # Popen + asyncio.to_thread(readline) approach consumed two pool
            # workers per child (readline + wait); with N>=5 children the pool
            # was exhausted and the unlucky child's stderr was never drained,
            # which eventually wedged the child on a blocking write to the
            # full pipe buffer.
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self.process_info = ProcessInfo(
                process=process,
                start_time=datetime.now(),
                args=args
            )

            self._is_running = True
            self._drain_tasks = [
                asyncio.create_task(self._drain_stream(process.stdout, "stdout")),
                asyncio.create_task(self._drain_stream(process.stderr, "stderr")),
            ]
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

            # Mark that we're stopping gracefully so _monitor_crash doesn't treat SIGTERM as crash
            self._stopping_gracefully = True

            if proc.returncode is None:
                proc.terminate()

            force_killed = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.terminate_delay)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    self.logger.warning(
                        f"Force killing {self.service_id} - did not terminate in {self.terminate_delay}s"
                    )
                    proc.kill()
                    await proc.wait()
                    force_killed = True

            # Drain tasks finish naturally when the child closes its streams.
            # Give them a brief moment to flush remaining lines, then cancel.
            if self._drain_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self._drain_tasks, return_exceptions=True),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    for task in self._drain_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*self._drain_tasks, return_exceptions=True)
                self._drain_tasks = []

            if self._crash_monitor_task:
                self._crash_monitor_task.cancel()
                try:
                    await self._crash_monitor_task
                except asyncio.CancelledError:
                    pass

            # Publish STOP event - subprocess was terminated by us
            # The crash monitor was cancelled, so we publish the event here
            reason = "force_killed" if force_killed else "terminated"
            exit_code = -9 if force_killed else 0
            await self._publish_stop_event(reason=reason, exit_code=exit_code)

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
            returncode = await self.process_info.process.wait()

            # Process exited! Clear process_info immediately to prevent duplicate handling
            if self.process_info is not None:
                process_info = self.process_info
                self.process_info = None

                # Check if it's a clean exit
                # Exit code 0 = clean
                # Exit code -15 (SIGTERM) or -2 (SIGINT) = clean if we initiated stop
                is_clean_exit = (
                    returncode == 0 or
                    (self._stopping_gracefully and returncode in (-15, -2))
                )

                if is_clean_exit:
                    reason = "completed" if returncode == 0 else "terminated"
                    self.logger.info(
                        f"Service {self.service_id} exited cleanly "
                        f"(exit code: {returncode}, reason: {reason})"
                    )
                    # Publish STOP event for clean exit
                    await self._publish_stop_event(reason=reason, exit_code=returncode)
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

    def _parse_log_level(self, line: str) -> tuple[int, str]:
        """Parse log level from subprocess log line.

        Args:
            line: Log line from subprocess (e.g., "[INFO ] svc|...: message")

        Returns:
            Tuple of (log_level_int, line) where log_level_int is logging.INFO, etc.
        """
        import logging
        import re

        # Try to extract log level from format: [LEVEL ] or [LEVEL]
        match = re.match(r'\[(\w+)\s*\]', line)
        if match:
            level_str = match.group(1).upper()
            level_map = {
                'DEBUG': logging.DEBUG,
                'INFO': logging.INFO,
                'WARNING': logging.WARNING,
                'WARN': logging.WARNING,
                'ERROR': logging.ERROR,
                'CRITICAL': logging.CRITICAL,
            }
            return level_map.get(level_str, logging.INFO), line

        # No recognizable log level, default to INFO
        return logging.INFO, line

    async def _drain_stream(self, stream: asyncio.StreamReader | None, label: str) -> None:
        """Drain a child stream line-by-line, forwarding through this runner's logger.

        Uses the asyncio StreamReader natively so a stalled drain task never
        blocks a thread-pool worker. Exits when the child closes the stream
        (EOF on subprocess exit) or when cancelled.
        """
        if stream is None:
            return

        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not decoded:
                    continue
                level, message = self._parse_log_level(decoded)
                self.logger.log(level, message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"Log drain error for {self.service_id} ({label}): {e}")


class ProcessLauncher(BaseLauncher):
    """Launcher that manages services as separate processes."""

    def __init__(self, launcher_id: str | None = None, terminate_delay: float = 5.0):
        # Use provided launcher_id or default to simple name
        if launcher_id is None:
            launcher_id = "process-launcher"

        super().__init__(launcher_id)
        self.terminate_delay = terminate_delay

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
    import argparse
    import os
    import socket

    def customize_parser(base_parser):
        """Customize parser for process launcher."""
        parser = argparse.ArgumentParser(
            description="Start TCS process launcher (each service in separate subprocess)",
            parents=[base_parser]
        )
        parser.add_argument(
            "--terminate-delay",
            type=float,
            default=5.0,
            help="Time to wait for graceful shutdown before force-kill (default: 5.0s)"
        )
        return parser

    def factory(launcher_id, args):
        """Create ProcessLauncher with terminate_delay from args."""
        # Generate proper launcher ID
        config_file = BaseLauncher.determine_config_file(args.config)
        launcher_id = BaseLauncher.gen_launcher_name(
            "process-launcher",
            config_file,
            os.getcwd(),
            socket.gethostname()
        )
        return ProcessLauncher(launcher_id=launcher_id, terminate_delay=args.terminate_delay)

    await BaseLauncher.launch(factory, customize_parser)


def main():
    """Entry point for process launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
