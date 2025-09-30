"""Asyncio-based launcher for running services in same process.

This launcher runs all services within the same Python process using asyncio,
suitable for development and resource-constrained environments.
"""

import asyncio
import logging
import signal
from typing import Dict, Optional, Any
from datetime import datetime

from ocabox_tcs.launchers.base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.config import ServicesConfigFile
from ocabox_tcs.management.service_controller import ServiceController


class AsyncioRunner(BaseRunner):
    """Runner that manages a service within the same process using asyncio."""

    def __init__(self, config: ServiceRunnerConfig):
        super().__init__(config)
        self.controller: Optional[ServiceController] = None
        self.start_time: Optional[datetime] = None

    async def start(self) -> bool:
        """Start service in current process."""
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            module_name = f"ocabox_tcs.services.{self.config.service_type}"
            instance_id = self.config.instance_context or self.config.service_type

            self.controller = ServiceController(
                module_name=module_name,
                instance_id=instance_id
            )

            if not await self.controller.initialize(config_file=self.config.config_file):
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

    async def get_status(self) -> Dict[str, Any]:
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

    def __init__(self, launcher_id: str = "asyncio-launcher"):
        super().__init__(launcher_id)
        self._shutdown_event = asyncio.Event()

    async def initialize(self, config: ServicesConfigFile) -> bool:
        """Initialize launcher from services config file.

        Args:
            config: ServicesConfigFile instance

        Returns:
            True if initialization successful
        """
        try:
            for service_cfg in config['services']:
                runner_config = ServiceRunnerConfig(
                    service_type=service_cfg['type'],
                    instance_context=service_cfg.get('instance_context'),
                    config_file=config.source,
                    runner_id=f"{self.launcher_id}.{service_cfg['type']}"
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
        """Shutdown all services."""
        self.logger.info("Stopping all services...")
        await self.stop_all()
        self._shutdown_event.set()


async def amain():
    """Asyncio launcher entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    config = ServicesConfigFile()
    config.load_config()

    launcher = AsyncioLauncher()
    if not await launcher.initialize(config):
        logging.error("Failed to initialize launcher")
        return

    if not await launcher.start_all():
        logging.error("Failed to start services")
        await launcher.stop_all()
        return

    await launcher.run()


def main():
    """Entry point for asyncio launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()