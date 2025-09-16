# base_service.py
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio

from markdown_it.common.html_re import attribute

from serverish.messenger import Messenger

from ocabox_tcs.config import ServicesConfigFile



@dataclass
class BaseServiceConfig:
    """Base configuration for all services"""

    instance_context: str # Service instance context (e.g., telescope ID)
    type: str = ""  # Service type identifier (must be overridden by subclass)
    log_level: str = "INFO" # Logging level name (e.g., "INFO", "DEBUG")
    nats_host: str = "nats.oca.lan" # NATS server host
    nats_port: int = 4222 # NATS server port

    def _load(self, config_file: str, instance_context: str):
        self.instance_context = instance_context

        """Load configuration from file"""
        config = ServicesConfigFile()
        config.load_config(config_file)

        # Find config section
        svc_config = None
        for c in config['services']:
            if c['type'] == self.type and c.get('instance_context', '') == instance_context:
                svc_config = c
                break

        if svc_config is None:
            raise ValueError(f'Service {self.type}:{instance_context} not found in config file {config_file}')

        ## update self with svc_config dict. Use .param.update() instead of .set_param()
        # Update fields from the selected service config
        for key, value in svc_config.items():
            setattr(self, key, value)

        ## update NATS host and port
        try:
            self.nats_host = config['nats']['host']
            self.nats_port = config['nats']['port']
        except KeyError:
            pass

    @property
    def id(self) -> str:
        return f'{self.type}:{self.instance_context}'


class BaseService(ABC):
    """Base class for OCM automation services"""

    ServiceConfigClass = BaseServiceConfig

    def __init__(self, config: BaseServiceConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.messenger: Optional[Messenger] = None
        self.running = False
        self._status: Dict[str, Any] = {"state": "init"}

    def _setup_logger(self) -> logging.Logger:
        """Setup service logger"""
        logger = logging.getLogger(f"svc:{self.config.id}")
        logger.setLevel(self.config.log_level)
        return logger

    async def start(self):
        """Initialize and start the service - called by lifecycle management"""
        try:
            # Initialize NATS messenger
            self.messenger = Messenger(self.config.id)

            # Start heartbeat and status tasks
            self.running = True
            asyncio.create_task(self._heartbeat_loop())
            asyncio.create_task(self._status_loop())

            # Start service-specific tasks
            await self._start_service()

            self.logger.info("Service started")

        except Exception as e:
            self.logger.error(f"Failed to start service: {str(e)}")
            raise

    async def stop(self):
        """Stop the service - called by lifecycle management"""
        self.running = False
        try:
            await self._stop_service()
            # if self.tic_client:
            #     await self.tic_client.disconnect()
            if self.messenger:
                await self.messenger.close()
            self.logger.info("Service stopped")
        except Exception as e:
            self.logger.error(f"Error stopping service: {str(e)}")
            raise

    async def _heartbeat_loop(self):
        """Publish heartbeat"""
        while self.running:
            try:
                # await self.messenger.publish(
                #     f"tic.status.{self.config.telescope_id}.{self.config.service_type}.heartbeat",
                #     {
                #         "timestamp": datetime.now().isoformat(),
                #         "status": self._status
                #     }
                # )
                await asyncio.sleep(5)
            except Exception as e:
                self.logger.error(f"Error in heartbeat loop: {str(e)}")
                await asyncio.sleep(5)

    async def _status_loop(self):
        """Publish detailed status"""
        while self.running:
            try:
                # await self.messenger.publish(
                #     f"tic.status.{self.config.telescope_id}.{self.config.service_type}",
                #     self._status
                # )
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.error(f"Error in status loop: {str(e)}")
                await asyncio.sleep(1)

    @abstractmethod
    async def _start_service(self):
        """Service-specific startup logic - to be implemented by subclasses"""
        pass

    @abstractmethod
    async def _stop_service(self):
        """Service-specific cleanup logic - to be implemented by subclasses"""
        pass

    @classmethod
    def app(cls):
        """Application entry point"""
        cls.main()


    @staticmethod
    def main():
        """Main service entry point"""
        import argparse
        parser = argparse.ArgumentParser(description="Start an OCM automation service.")
        parser.add_argument("config_file", type=str, help="Path to the config file")
        parser.add_argument("service_type", type=str, help="Type of the service - module name")
        parser.add_argument("service_id", type=str, help="Service instance context/ID")
        args = parser.parse_args()

        config_file = args.config_file
        service_type = args.service_type
        service_id = args.service_id

        svc_cls = getattr(__import__(f"ocabox_tcs.services.{service_type}", fromlist=["service_class"]), "service_class")
        cfg_cls = getattr(__import__(f"ocabox_tcs.services.{service_type}", fromlist=["config_class"]), "config_class")

        config = cfg_cls(instance_context=service_id)
        config._load(config_file, service_id)
        logging.basicConfig(level=config.log_level)
        service = svc_cls(config)
        asyncio.run(service.start())
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            asyncio.run(service.stop())
            asyncio.get_event_loop().close()


