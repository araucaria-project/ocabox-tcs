"""Example permanent service using the new framework.

This demonstrates a continuously running service that logs periodically.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from ocabox_tcs.base_service import BaseServiceConfig, BaseBlockingPermanentService, service, config
from ocabox_tcs.services.dome_follower_svc.manager import Manager


@config('dome_follower_svc.dome_follower')
@dataclass
class DomeFollowerServiceConfig(BaseServiceConfig):
    """Configuration for DumbPermanent service."""
    interval: float = 1.0  # Interval in seconds
    turn_on_automatically: bool = False  # True is just for debug
    dome_speed: float = 30 # deg / sec
    follow_tolerance: float = 3.0 # deg
    settle_time: float = 3.0 # sec


@service('dome_follower_svc.dome_follower')
class DomeFollowerService(BaseBlockingPermanentService):
    """Simple service that logs a message periodically."""
    def __init__(self):
        super().__init__()
        self.manager: Optional[Manager] = None


    async def on_start(self):
        """Setup before main loop starts."""
        self.svc_logger.info(
            f"Starting dome follower service, with interval: {self.svc_config.interval}"
        )
        self.manager = Manager(service=self, config=self.svc_config)
        await self.manager.start_comm()
        await self.manager.set_follow_params()
        await self.manager.set_mount_type_params()
        if self.svc_config.turn_on_automatically:
            self.manager.follow_on = True
            self.svc_logger.warning(
                f"Dome follower configured to turned on automatically "
                f"(deprecated method, use rpc instead)"
            )

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            try:
                ts_0 = time.time()
                await self.manager.dome_follow()
                await asyncio.sleep(self.svc_config.interval)
                self.manager.turn_time = time.time() - ts_0
            except asyncio.CancelledError:
                break


    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.svc_logger.info(f"Stopping service dome follower")
        await self.manager.stop_comm()


if __name__ == '__main__':
    DomeFollowerService.main()
