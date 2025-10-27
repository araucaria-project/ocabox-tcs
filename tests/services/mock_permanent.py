"""Mock permanent service for testing.

A simple permanent service with controllable behavior via environment variables.
Used for testing launcher lifecycle, monitoring, and status management.

Environment Variables:
    MOCK_WORK_INTERVAL: Sleep interval in main loop (default: 0.5 seconds)
    MOCK_STARTUP_DELAY: Delay during startup (default: 0.0 seconds)
    MOCK_SHUTDOWN_DELAY: Delay during shutdown (default: 0.0 seconds)
    MOCK_HEALTHCHECK_STATUS: Override healthcheck status (ok/degraded/warning/error)
    MOCK_HEALTHCHECK_MESSAGE: Custom healthcheck message
    MOCK_WORK_COUNT: Number of work iterations before stopping (0 = infinite)
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, config, service
from ocabox_tcs.monitoring import Status

logger = logging.getLogger(__name__)


@dataclass
@config
class MockPermanentConfig:
    """Configuration for mock permanent service."""
    work_interval: float = 0.5  # Sleep interval in main loop
    startup_delay: float = 0.0  # Delay during startup
    shutdown_delay: float = 0.0  # Delay during shutdown
    healthcheck_status: str | None = None  # Override healthcheck status
    healthcheck_message: str = "Mock service running"
    work_count: int = 0  # Number of iterations (0 = infinite)


@service
class MockPermanentService(BaseBlockingPermanentService):
    """Mock permanent service for testing.

    Simple service that runs in a loop with configurable behavior.
    Useful for testing launcher lifecycle, monitoring, and status changes.
    """

    config: MockPermanentConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iteration_count = 0

    async def on_start(self):
        """Called before run_service starts - handle startup delay."""
        if self.config.startup_delay > 0:
            self.logger.info(f"Startup delay: {self.config.startup_delay}s")
            await asyncio.sleep(self.config.startup_delay)

    async def on_stop(self):
        """Called after run_service stops - handle shutdown delay."""
        if self.config.shutdown_delay > 0:
            self.logger.info(f"Shutdown delay: {self.config.shutdown_delay}s")
            await asyncio.sleep(self.config.shutdown_delay)

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            self.iteration_count += 1
            self.logger.debug(f"Mock work iteration {self.iteration_count}")

            # Check if we should stop after N iterations
            if self.config.work_count > 0 and self.iteration_count >= self.config.work_count:
                self.logger.info(f"Reached work_count limit ({self.config.work_count}), stopping")
                break

            await asyncio.sleep(self.config.work_interval)

    def healthcheck(self) -> Status:
        """Custom healthcheck with configurable status."""
        # Override via config if specified
        if self.config.healthcheck_status:
            status_str = self.config.healthcheck_status.upper()
            if hasattr(Status, status_str):
                status = getattr(Status, status_str)
                self.logger.debug(f"Healthcheck override: {status}")
                return status

        # Default: OK
        return Status.OK


if __name__ == '__main__':
    # Use base class's main() - it handles test services via config's module field
    MockPermanentService.main()
