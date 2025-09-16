"""
Development replacement for systemd service management

Best for local launching set of services during development.
"""

import logging
import asyncio
import os
import signal
import sys
from datetime import datetime
from typing import Dict, Optional, List
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
    instance_context: str = ""
    status: str = "init"
    error: Optional[str] = None
    start_time: Optional[datetime] = None
    args: Optional[List[str]] = None

class DevServiceLauncher:
    __doc__ = __doc__

    def __init__(self):
        self.services: Dict[str, ServiceProcess] = {}
        self.logger = logging.getLogger("dev-launcher")
        self.messenger: Optional[Messenger] = None
        self.terminate_delay = 1  # [s] how long to wait before force killing a service

    async def start_service(self, service_type: str, instance_context: str | None, config_file: Optional[str] = None):
        """Start service in subprocess"""
        if instance_context is None:
            service_id = service_type
        else:
            service_id = f"{service_type}-{instance_context}"

        if service_id in self.services:
            self.logger.warning(f"Service {service_id} already running")
            return

        try:
            # Start service process
            args = [
                "poetry", "run", "python", "-m",
                f"ocabox_tcs.services.{service_type}",
                os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config", config_file)) if config_file else "",
                service_type
            ]
            if instance_context is not None:
                args += [instance_context]

            logging.info(f"Starting service {service_id}: {' '.join(args)}")
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self.services[service_id] = ServiceProcess(
                process=process,
                service_type=service_type,
                instance_context=instance_context,
                start_time = datetime.now(),
                args=args
            )

            # Start log monitoring
            asyncio.create_task(self._monitor_logs(service_id))
            self.logger.info(f"Service {service_id} started")
        except Exception as e:
            self.logger.error(f"Failed to start {service_id}: {str(e)}", exc_info=True)


    async def stop_service(self, service_id: str):
        """Stop service subprocess"""
        if service_id in self.services:
            self.logger.info(f"Stopping {service_id}")
            try:
                proc = self.services[service_id].process
                if proc is not None:
                    proc.terminate()
                    await asyncio.sleep(self.terminate_delay)
                    if proc.poll() is None:  # still running
                        self.logger.warning(f"Force killing {service_id} as it not terminated in {self.terminate_delay}s")
                        proc.kill()
                del self.services[service_id]
                self.logger.info(f"Service {service_id} stopped")
            except Exception as e:
                self.logger.error(f"Failed to stop {service_id}: {str(e)}")
        else:
            self.logger.warning(f"Service {service_id} not running")

    async def _monitor_logs(self, service_id: str):
        """Monitor service logs"""
        service = self.services[service_id]
        while True:
            # Offload blocking readline to a thread to avoid blocking the event loop
            line = await asyncio.to_thread(service.process.stderr.readline)
            if not line:
                break
            self.logger.info(f"[{service_id}] {line.strip()}")


async def amain():
    manager = DevServiceLauncher()
    may_exit = asyncio.Event()

    # Handle termination
    def handle_signal(sig, frame=None):
        logging.info(f"Received signal {sig}, cleaning up...")
        asyncio.create_task(cleanup())

    async def cleanup():
        logging.info("Closing services...")
        for service_id in list(manager.services.keys()):
            logging.info(f"Closing service {service_id}")
            await manager.stop_service(service_id)
            logging.info(f"Service {service_id} stopped")
        logging.info("All services stopped")
        may_exit.set()

    loop = asyncio.get_running_loop()
    # signal.signal(signal.SIGINT, handle_signal)
    # signal.signal(signal.SIGTERM, handle_signal)
    # Register asyncio-native signal handlers (Unix/macOS)
    loop.add_signal_handler(signal.SIGINT, lambda: handle_signal("SIGINT"))
    loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal("SIGTERM"))


    # Load config from relative path ../../config and start services
    config = ServicesConfigFile()
    config.load_config()

    for service in config['services']:
        await manager.start_service(
            service_type=service['type'],
            instance_context=service.get('instance_context'),
            config_file=config.source
        )

    # Wait for termination (by SIGINT/SIGTERM)
    logging.info("Services started, running. Press Ctrl+C to stop.")
    await may_exit.wait()
    logging.info("Services stopped.")

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()


