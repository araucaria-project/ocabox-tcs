"""This is exmaple dumb service, designed to run permanently

Programm runner, guider or dome-follower probalby should be implemented in this way
"""

import logging
import asyncio

import param

from ocabox_tcs.base_service import BaseService, ServiceConfig
logger = logging.getLogger('dumb-svc')

class DumbPermanentServiceConfig(ServiceConfig):
    interval = param.Number(default=1, doc="Interval in seconds")


class DumbPermanentService(BaseService):
    ServiceConfigClass = DumbPermanentServiceConfig

    async def _start_service(self):
        """dumb loop writing log"""
        self.logger.info("Starting dumb service, with interval: %s", self.config.interval)
        while True:
            logger.info("I'm still alive")
            await asyncio.sleep(self.config.interval)

    async def _stop_service(self):
        self.logger.info("Stopping dumb service")


service_class = DumbPermanentService
config_class = DumbPermanentServiceConfig


# run:
#       python dumb_permanent /abs-path/ocabox-tcs/config/services.yaml dumb_permanent dumb

if __name__ == '__main__':
    DumbPermanentService.app()
