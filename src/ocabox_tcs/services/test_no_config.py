"""Test service without custom config - testing optional config."""

import asyncio
from ..base_service import BasePermanentService, service


@service("test_no_config")
class TestNoConfigService(BasePermanentService):
    """Service without custom config class."""
    
    def __init__(self):
        super().__init__()
        self._task: asyncio.Task = None
    
    async def start_service(self):
        self.logger.info("Starting service without custom config")
        self.logger.info(f"Using base config with type: {self.config.type}")
        self._task = asyncio.create_task(self._run_loop())
    
    async def stop_service(self):
        self.logger.info("Stopping service without custom config")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def _run_loop(self):
        count = 0
        while self.is_running and count < 2:
            self.logger.info(f"Running without custom config (count: {count})")
            await asyncio.sleep(1)
            count += 1