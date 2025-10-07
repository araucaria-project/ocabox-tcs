"""Service demonstrating logging best practices.

This example shows how to use different log levels effectively:
- DEBUG: Detailed diagnostic information
- INFO: Normal operational messages
- WARNING: Warning messages for unusual situations
- ERROR: Error messages for failures

Run standalone:
    python src/ocabox_tcs/services/examples/03_logging.py config/examples.yaml logging

Run with launchers:
    poetry run tcs_asyncio --config config/examples.yaml
    poetry run tcs_process --config config/examples.yaml
"""
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service


@config
@dataclass
class LoggingConfig(BaseServiceConfig):
    """Configuration for logging service."""
    interval: float = 3.0
    simulate_errors: bool = True


@service
class LoggingService(BaseBlockingPermanentService):
    """Service demonstrating logging best practices."""

    async def on_start(self):
        """Called before main loop starts."""
        self.cycle = 0
        self.logger.info("Logging service starting up")
        self.logger.debug("Debug mode enabled - verbose output")

    async def run_service(self):
        """Main service loop with various log levels."""
        while self.is_running:
            self.cycle += 1

            # Normal operation - INFO level
            self.logger.info(f"Processing cycle {self.cycle}")

            # Detailed diagnostic - DEBUG level
            self.logger.debug(f"Cycle details: interval={self.config.interval}s")

            # Warning for unusual situations
            if self.cycle % 5 == 0:
                self.logger.warning(f"Cycle {self.cycle} - entering maintenance mode")

            # Error simulation
            if self.config.simulate_errors and self.cycle % 10 == 0:
                try:
                    # Simulate an error
                    raise ValueError("Simulated error for demonstration")
                except ValueError as e:
                    self.logger.error(f"Caught error in cycle {self.cycle}: {e}")

            await asyncio.sleep(self.config.interval)

    async def on_stop(self):
        """Called after main loop stops."""
        self.logger.info(f"Logging service stopping after {self.cycle} cycles")


if __name__ == '__main__':
    LoggingService.main()
