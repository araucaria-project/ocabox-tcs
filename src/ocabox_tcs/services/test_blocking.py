"""Test service demonstrating BaseBlockingPermanentService."""

import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, service, config


@config("test_blocking")
@dataclass
class TestBlockingConfig(BaseServiceConfig):
    """Configuration for blocking test service."""
    interval: int = 2
    max_iterations: int = 3


@service("test_blocking")
class TestBlockingService(BaseBlockingPermanentService):
    """Example service using BaseBlockingPermanentService."""
    
    async def on_start(self):
        """Setup before main loop starts."""
        self.logger.info("Setting up blocking service")
        self._iteration_count = 0
    
    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.logger.info("Cleaning up blocking service")
    
    async def run_service(self):
        """Main blocking service loop."""
        self.logger.info("Starting main service loop")
        
        while self.is_running and self._iteration_count < self.config.max_iterations:
            self.logger.info(f"Blocking service iteration {self._iteration_count + 1}")
            self._iteration_count += 1
            
            # Simulate work
            await asyncio.sleep(self.config.interval)
        
        self.logger.info("Main service loop finished")