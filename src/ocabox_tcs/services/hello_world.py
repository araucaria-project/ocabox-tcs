"""Hello World service - canonical template for TCS services.

This service demonstrates the recommended patterns for service implementation.
Status management is automatic - the framework handles STARTUP/OK/SHUTDOWN transitions.
"""

import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service


@config
@dataclass
class HelloWorldConfig(BaseServiceConfig):
    """Configuration for HelloWorld service."""
    interval: int = 5
    message: str = "Hello World!"


@service
class HelloWorldService(BaseBlockingPermanentService):
    """Simple service that logs a message periodically.

    Status management is fully automatic:
    - Controller sets STARTUP during initialization
    - Controller sets OK after start_service() completes
    - Controller sets SHUTDOWN during stop_service()
    - Healthcheck returns OK by default (can be overridden for custom checks)
    """

    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info(f"Hello World service ready with {self.config.interval}s interval")

    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.logger.info("Hello World service cleanup complete")

    async def run_service(self):
        """Main service loop - framework handles task management."""
        while self.is_running:
            try:
                self.logger.info(f"{self.config.message} (interval: {self.config.interval}s)")
                await asyncio.sleep(self.config.interval)
            except asyncio.CancelledError:
                # Expected when service is stopped
                break
            except Exception as e:
                self.logger.error(f"Error in hello loop: {e}")
                await asyncio.sleep(min(self.config.interval, 10))


if __name__ == '__main__':
    HelloWorldService.main()
