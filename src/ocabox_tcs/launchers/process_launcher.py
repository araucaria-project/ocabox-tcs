"""Process-based launcher for running services as separate processes.

This launcher spawns each service in its own subprocess, suitable for
development and testing environments.
"""

import asyncio
import os
import subprocess
import signal
from datetime import datetime
from typing import Dict, Optional, Any, List
from dataclasses import dataclass

from .base_launcher import BaseLauncher, BaseRunner, ServiceRunnerConfig
from ocabox_tcs.config import ServicesConfigFile


@dataclass
class ProcessInfo:
    """Information about a running service process."""
    process: subprocess.Popen
    start_time: datetime
    args: List[str]


class ProcessRunner(BaseRunner):
    """Runner that manages a service in a subprocess."""

    def __init__(self, config: ServiceRunnerConfig, terminate_delay: float = 1.0):
        super().__init__(config)
        self.process_info: Optional[ProcessInfo] = None
        self.terminate_delay = terminate_delay
        self._log_monitor_task: Optional[asyncio.Task] = None

    async def start(self) -> bool:
        """Start service in subprocess."""
        if self._is_running:
            self.logger.warning(f"Service {self.service_id} already running")
            return False

        try:
            args = [
                "poetry", "run", "python", "-m",
                f"ocabox_tcs.services.{self.config.service_type}",
            ]

            if self.config.config_file:
                config_path = os.path.abspath(self.config.config_file)
                args.append(config_path)

            args.append(self.config.service_type)

            if self.config.instance_context:
                args.append(self.config.instance_context)

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
            await asyncio.sleep(self.terminate_delay)

            if proc.poll() is None:
                self.logger.warning(
                    f"Force killing {self.service_id} - did not terminate in {self.terminate_delay}s"
                )
                proc.kill()

            if self._log_monitor_task:
                self._log_monitor_task.cancel()
                try:
                    await self._log_monitor_task
                except asyncio.CancelledError:
                    pass

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

    async def get_status(self) -> Dict[str, Any]:
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

    def __init__(self, launcher_id: str = "process-launcher", terminate_delay: float = 1.0):
        super().__init__(launcher_id)
        self.terminate_delay = terminate_delay
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

                runner = ProcessRunner(runner_config, terminate_delay=self.terminate_delay)
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

        self.logger.info("Services started. Press Ctrl+C to stop.")
        await self._shutdown_event.wait()
        self.logger.info("Launcher shutdown complete")

    async def _shutdown(self):
        """Shutdown all services."""
        self.logger.info("Stopping all services...")
        await self.stop_all()
        self._shutdown_event.set()


async def amain():
    """Process launcher entry point."""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    config = ServicesConfigFile()
    config.load_config()

    launcher = ProcessLauncher()
    if not await launcher.initialize(config):
        logging.error("Failed to initialize launcher")
        return

    if not await launcher.start_all():
        logging.error("Failed to start services")
        await launcher.stop_all()
        return

    await launcher.run()


def main():
    """Entry point for process launcher."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()