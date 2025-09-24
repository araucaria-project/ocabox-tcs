"""Hello World service - minimal example of the new service framework."""

import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, service, config
from ocabox_tcs.monitoring import Status


# @config("hello_world")
@config
@dataclass
class HelloWorldConfig(BaseServiceConfig):
    """Configuration for HelloWorld service."""
    interval: int = 5
    message: str = "Hello World!"


# @service("hello_world")
@service
class HelloWorldService(BaseBlockingPermanentService):
    """Simple service that logs a message periodically."""
    
    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info("Setting up Hello World service")
        
        # Set up monitoring
        self.monitor.set_status(Status.STARTUP, "Starting Hello World service")
        self.monitor.add_healthcheck_cb(self.healthcheck)

        self.monitor.set_status(Status.OK, "Hello World service running")
        self.logger.info(f"Hello World service ready with {self.config.interval}s interval")
    
    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.monitor.set_status(Status.SHUTDOWN, "Stopping Hello World service")
        self.logger.info("Hello World service stopped")
    
    async def run_service(self):
        """Main service loop - framework handles task management."""
        self.logger.info("Starting Hello World main loop")
        
        while self.is_running:
            try:
                self.logger.info(f"{self.config.message} (interval: {self.config.interval}s)")
                await asyncio.sleep(self.config.interval)
            except asyncio.CancelledError:
                # Expected when service is stopped
                break
            except Exception as e:
                self.logger.error(f"Error in hello loop: {e}")
                self.monitor.set_status(Status.ERROR, f"Loop error: {e}")
                await asyncio.sleep(min(self.config.interval, 10))
        
        self.logger.info("Hello World main loop finished")
    
    def healthcheck(self) -> Status:
        """Check if service is healthy."""
        if not self.is_running:
            return Status.SHUTDOWN
        
        # BaseBlockingPermanentService handles task management,
        # so we just check if we're running
        return Status.OK


if __name__ == '__main__':
    HelloWorldService.main()
