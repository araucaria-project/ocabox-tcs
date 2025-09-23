"""Test service without decorators - testing legacy support."""

import asyncio
from dataclasses import dataclass

from ..base_service import BasePermanentService, BaseServiceConfig
from ..monitoring import Status


@dataclass  
class TestLegacyConfig(BaseServiceConfig):
    """Config without decorator."""
    message: str = "Legacy service works!"


class TestLegacyService(BasePermanentService):
    """Service without decorator."""
    
    def __init__(self):
        super().__init__()
        self._task: asyncio.Task = None
    
    async def start_service(self):
        self.logger.info("Starting legacy service")
        self._task = asyncio.create_task(self._run_loop())
    
    async def stop_service(self):
        self.logger.info("Stopping legacy service")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def _run_loop(self):
        while self.is_running:
            self.logger.info(self.config.message)
            await asyncio.sleep(2)


# Legacy exports
service_class = TestLegacyService
config_class = TestLegacyConfig