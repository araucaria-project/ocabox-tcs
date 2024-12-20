# base_service.py
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio

import param
import typer

from serverish.messenger import Messenger

from ocabox_tcs.config import ServicesConfigFile


class ServiceConfig(param.Parameterized):
    """Base configuration for all services"""
    id: str = param.String(doc="Service identifier")
    type: str = param.String(doc="Service type - corresponds to file name")
    log_level: str = param.String(default="INFO", doc="Logging level")
    nats_host: str = param.String(default="nats.oca.lan", doc="NATS server host")
    nats_port: int = param.Integer(default=4222, doc="NATS server port")

    def _load(self, config_file: str, id: str):
        self.id = id

        """Load configuration from file"""
        config = ServicesConfigFile()
        config.load_config(config_file)

        try:
            svc_config = config['services'].get(id)
        except KeyError:
            raise ValueError(f"Service {id} not found in config file {config_file}")

        ## update self with svc_config dict. Use .param.update() instead of .set_param()
        self.param.update(svc_config)

        ## update NATS host and port
        self.nats_host = config['nats']['host']
        self.nats_port = config['nats']['port']


class BaseService(ABC):
    """Base class for OCM automation services"""

    ServiceConfigClass = ServiceConfig
    app = typer.Typer()

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.messenger: Optional[Messenger] = None
        # self.tic_client: Optional[ClientAPI] = None
        self.running = False
        self._status: Dict[str, Any] = {"state": "init"}
        
    def _setup_logger(self) -> logging.Logger:
        """Setup service logger"""
        logger = logging.getLogger(f"svc:{self.config.id}")
        logger.setLevel(self.config.log_level)
        return logger

    async def start(self):
        """Initialize and start the service"""
        try:
            # Initialize NATS messenger
            self.messenger = Messenger(self.config.id)
            
            # # Initialize TIC client
            # self.tic_client = ClientAPI(telescope_id=self.config.telescope_id)
            # await self.tic_client.connect()
            
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
        """Stop the service"""
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
        """Service-specific startup logic"""
        pass

    @abstractmethod
    async def _stop_service(self):
        """Service-specific cleanup logic"""
        pass

    @staticmethod
    @app.command()
    def main(config_file: str, service_type: str, service_id: str):
        """Main service entry point"""

        svc_cls = getattr(__import__(f"ocabox_tcs.services.{service_type}", fromlist=["service_class"]), "service_class")
        cfg_cls = getattr(__import__(f"ocabox_tcs.services.{service_type}", fromlist=["config_class"]), "config_class")


        config = cfg_cls()
        config._load(config_file, service_id)
        logging.basicConfig(level=config.log_level)
        service = svc_cls(config)
        asyncio.run(service.start())
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            asyncio.run(service.stop())
            asyncio.get_event_loop().close()


