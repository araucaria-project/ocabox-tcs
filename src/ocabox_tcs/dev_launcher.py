import logging
import asyncio
import os
import signal
import sys
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass
import subprocess
import yaml

from serverish.messenger import Messenger

from ocabox_tcs.config import ServicesConfigFile

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

@dataclass
class ServiceProcess:
    process: Optional[subprocess.Popen] = None
    service_type: str = ""
    telescope_id: str = ""
    status: str = "init"
    error: Optional[str] = None
    start_time: Optional[datetime] = None

class DevServiceLauncher:
    """Development replacement for systemd service management"""

    def __init__(self):
        self.services: Dict[str, ServiceProcess] = {}
        self.logger = logging.getLogger("dev-launcher")
        self.messenger: Optional[Messenger] = None
        self.terminate_delay = 1  # [s] how long to wait before force killing a service

    async def start_service(self, service_type: str, telescope_id: str):
        """Start service in subprocess"""
        service_id = f"{service_type}-{telescope_id}"

        if service_id in self.services:
            self.logger.warning(f"Service {service_id} already running")
            return
        self.logger.info(f"Starting {service_id}")

        try:
            # Start service process
            process = subprocess.Popen(
                [
                    "poetry", "run", "python", "-m",
                    f"ocabox_tas.services.{service_type}",
                    "--telescope", telescope_id
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self.services[service_id] = ServiceProcess(
                process=process,
                service_type=service_type,
                telescope_id=telescope_id,
                start_time = datetime.now()
            )

            # Start log monitoring
            asyncio.create_task(self._monitor_logs(service_id))
        except Exception as e:
            self.logger.error(f"Failed to start {service_id}: {str(e)}")
        self.logger.info(f"Service {service_id} started")

    async def stop_service(self, service_id: str):
        """Stop service subprocess"""
        if service_id in self.services:
            self.logger.info(f"Stopping {service_id}")
            try:
                self.services[service_id].process.terminate()
                await asyncio.sleep(self.terminate_delay)
                if self.services[service_id].process.poll() is None: ## if process is still running
                    self.logger.warning(f"Force killing {service_id} as it not terminated in {self.terminate_delay}s")
                    self.services[service_id].process.kill()
                del self.services[service_id]
            except Exception as e:
                self.logger.error(f"Failed to stop {service_id}: {str(e)}")
            self.logger.info(f"Service {service_id} stopped")
        else:
            self.logger.warning(f"Service {service_id} not running")

    async def _monitor_logs(self, service_id: str):
        """Monitor service logs"""
        service = self.services[service_id]
        while True:
            line = service.process.stdout.readline()
            if not line:
                break
            self.logger.info(f"[{service_id}] {line.strip()}")


async def main():
    manager = DevServiceLauncher()

    # Handle termination
    def handle_signal(sig, frame):
        logging.info(f"Received signal {sig}, cleaning up...")
        asyncio.create_task(cleanup())

    async def cleanup():
        for service_id in list(manager.services.keys()):
            await manager.stop_service(service_id)
        logging.info("All services stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Load config from relative path ../../config and start services
    config = ServicesConfigFile()
    config.load_config()

    for service in config['services']:
        await manager.start_service(
            service['type'],
            service['telescope_id']
        )

    # Wait for termination
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())


