"""Test service without decorators - demonstrating legacy fallback support.

This service demonstrates the legacy export approach that still works
as a fallback when decorators are not used.

Modern services should use the @service and @config decorators instead.
"""

import asyncio
from dataclasses import dataclass

from ..base_service import BasePermanentService, BaseServiceConfig
from ..monitoring import Status


@dataclass
class TestLegacyConfig(BaseServiceConfig):
    """Config class without @config decorator - uses legacy exports."""
    message: str = "Legacy export approach still works!"


class TestLegacyService(BasePermanentService):
    """Service class without @service decorator - uses legacy exports.

    Service type will be "test_legacy" (derived from filename: test_legacy.py)
    """

    def __init__(self):
        super().__init__()
        self._task: asyncio.Task = None

    async def start_service(self):
        self.logger.info("Starting legacy service (fallback discovery)")
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


# Legacy exports - used as fallback when decorators are not present
# Service type will be "test_legacy" (from filename)
# Modern services should use @service and @config decorators instead
service_class = TestLegacyService
config_class = TestLegacyConfig


if __name__ == '__main__':
    TestLegacyService.main()