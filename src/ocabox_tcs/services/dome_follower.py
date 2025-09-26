"""Example permanent service using the new framework.

This demonstrates a continuously running service that logs periodically.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from ocabox_tcs.base_service import BaseServiceConfig, BaseBlockingPermanentService, service, config
from ocabox_tcs.services.dome_follower_svc.manager import Manager


@config
@dataclass
class DomeFollowerServiceConfig(BaseServiceConfig):
    """Configuration for DumbPermanent service."""
    interval: float = 1.0  # Interval in seconds


@service
class DomeFollowerService(BaseBlockingPermanentService):
    """Simple service that logs a message periodically."""
    def __init__(self):
        super().__init__()
        self.manager: Optional[Manager] = None


    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info(f"Starting dumb service, with interval: {self.config}")
        self.manager = Manager(service=self, config=self.config)
        await self.manager.start_comm()

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            try:
                self.logger.info("I'm still dooming")
                await asyncio.sleep(self.config.interval)
            except asyncio.CancelledError:
                break

    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.logger.info(f"Stopping service XXXXXXX")
        await self.manager.stop_comm()


if __name__ == '__main__':
    DomeFollowerService.main()