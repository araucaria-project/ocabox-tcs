"""Asyncio-based launcher for running services in same process.

This launcher runs all services within the same Python process using asyncio,
suitable for development and resource-constrained environments.
"""

import asyncio
import logging
from datetime import datetime
from time import time
from typing import Any

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.management.process_context import ProcessContext
from ocabox_tcs.management.service_controller import ServiceController
from ocabox_tcs.management.service_registry import ServiceRegistry


class AsyncioRunner(BaseRunner):
    """Runner that manages a service within the same process using asyncio."""

    def __init__(
        self,
        config: ServiceRunnerConfig,
        registry: ServiceRegistry,
        launcher_id: str | None = None,
        subject_prefix: str = "svc"
    ):
        super().__init__(config, launcher_id=launcher_id, subject_prefix=subject_prefix)
        self.registry = registry
        self.controller: ServiceController | None = None
        self.start_time: datetime | None = None
        self._crash_monitor_task: asyncio.Task | None = None
        self._stopping_gracefully: bool = False  # Track if we initiated stop

    async def start(self) -> bool:
        """Start service in current process.

        Note: ProcessContext must already be initialized by AsyncioLauncher.
        """
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            # Create ServiceController with service_type and variant
            self.controller = ServiceController(
                service_type=self.config.service_type,
                variant=self.config.variant,
                registry=self.registry,
                runner_id=self.config.runner_id,
                parent_name=self.config.parent_name
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
            # Mark that we're stopping gracefully so _monitor_crash doesn't warn
            self._stopping_gracefully = True

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
                    # Service stopped - check if we initiated it
                    # Clear controller immediately to prevent duplicate handling
                    controller = self.controller
                    self.controller = None

                    # If we initiated stop gracefully, just publish STOP and return
                    if self._stopping_gracefully:
                        self.logger.info(f"Service {self.service_id} stopped gracefully")
                        await self._publish_stop_event(reason="completed", exit_code=0)
                        self._is_running = False
                        return

                    # Service stopped unexpectedly - warn and check if we should restart
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

class AsyncioLauncher(BaseLauncher):
    """Launcher that manages services within the same process using asyncio."""

    def __init__(self, launcher_id: str | None = None):
        # Use provided launcher_id or default to simple name
        if launcher_id is None:
            launcher_id = "asyncio-launcher"

        super().__init__(launcher_id)

    def _get_launcher_type_display(self) -> str:
        """Get display name for banner."""
        return "Asyncio (all services in same process)"

    def _create_runner(
        self,
        config: ServiceRunnerConfig,
        registry: ServiceRegistry,
        subject_prefix: str
    ) -> AsyncioRunner:
        """Create AsyncioRunner for in-process execution."""
        return AsyncioRunner(
            config,
            registry=registry,
            launcher_id=self.launcher_id,
            subject_prefix=subject_prefix
        )


async def amain():
    """Asyncio launcher entry point."""
    import argparse
    import os
    import socket

    def customize_parser(base_parser):
        """Customize parser for asyncio launcher."""
        parser = argparse.ArgumentParser(
            description="Start TCS asyncio launcher (all services in same process)",
            parents=[base_parser]
        )
        return parser

    def factory(launcher_id, args):
        """Create AsyncioLauncher."""
        # Generate proper launcher ID
        config_file = BaseLauncher.determine_config_file(args.config)
        launcher_id = BaseLauncher.gen_launcher_name(
            "asyncio-launcher",
            config_file,
            os.getcwd(),
            socket.gethostname()
        )
        return AsyncioLauncher(launcher_id=launcher_id)

    await BaseLauncher.launch(factory, customize_parser)


def main():
    """Entry point for asyncio launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()