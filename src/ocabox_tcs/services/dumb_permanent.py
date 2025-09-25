"""Example permanent service using the new framework.

This demonstrates a continuously running service that logs periodically.
"""

import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseServiceConfig, BaseBlockingPermanentService, service, config


@config
@dataclass
class DumbPermanentServiceConfig(BaseServiceConfig):
    """Configuration for DumbPermanent service."""
    interval: float = 1.0  # Interval in seconds


@service
class DumbPermanentService(BaseBlockingPermanentService):
    """Simple service that logs a message periodically."""

    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info(f"Starting dumb service, with interval: {self.config.interval}")

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            try:
                self.logger.info("I'm still alive")
                await asyncio.sleep(self.config.interval)
            except asyncio.CancelledError:
                break

    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.logger.info("Stopping dumb service")



if __name__ == '__main__':
    DumbPermanentService.main()
