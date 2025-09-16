"""This is example dumb service, designed to run permanently

Program runner, guider or dome-follower probably should be implemented in this way
"""

import logging
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseServiceConfig
from ocabox_tcs.base_service_ocabox import BaseOCABoxService

logger = logging.getLogger('dumb-svc')

@dataclass
class DumbComplexServiceConfig(BaseServiceConfig):
    type: str = 'dumb_complex' # Obligatory override
    interval: float = 1 # Interval in seconds


class DumbComplexService(BaseOCABoxService):
    ServiceConfigClass = DumbComplexServiceConfig

    async def _start_service(self):
        """dumb loop writing log"""
        self.logger.info("Starting dumb-complex service, with interval: %s", self.config.interval)
        while True:
            logger.info("I'm still complex")
            await asyncio.sleep(self.config.interval)

    async def _stop_service(self):
        self.logger.info("Stopping dumb-complex service")


service_class = DumbComplexService
config_class = DumbComplexServiceConfig


# run:
#       python dumb_permanent /abs-path/ocabox-tcs/config/services.yaml dumb_permanent dumb

if __name__ == '__main__':
    DumbComplexService.app()
