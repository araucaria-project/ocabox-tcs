"""Example complex service using the new framework.

This demonstrates a complex service with sub-components.
"""

import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseServiceConfig, BaseBlockingPermanentService, service, config


@config
@dataclass
class DumbComplexServiceConfig(BaseServiceConfig):
    """Configuration for DumbComplex service."""
    interval: float = 1.0  # Interval in seconds


@service
class DumbComplexService(BaseBlockingPermanentService):
    """Complex service that logs a message periodically."""

    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info(f"Starting dumb-complex service, with interval: {self.config.interval}")

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            try:
                self.logger.info("I'm still complex")
                await asyncio.sleep(self.config.interval)
            except asyncio.CancelledError:
                break

    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.logger.info("Stopping dumb-complex service")



if __name__ == '__main__':
    DumbComplexService.main()
