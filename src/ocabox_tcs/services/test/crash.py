"""Test service for crash handling and restart policies.

This service is designed to crash after running for a configured duration,
allowing testing of restart policies and crash detection mechanisms.
"""

import asyncio
import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import service, config, BaseBlockingPermanentService, BaseServiceConfig


@config
@dataclass
class TestCrashConfig(BaseServiceConfig):
    """Configuration for crash test service."""
    run_duration: float = 3.0  # How long to run before crashing (seconds)
    exit_code: int = 1  # Exit code when crashing (0=success, 1=failure, >128=abnormal)
    crash_message: str = "Service crashing as configured"


@service
class TestCrashService(BaseBlockingPermanentService):
    """Service that crashes after a configured duration for testing restart policies."""

    config: TestCrashConfig

    async def run_service(self) -> None:
        """Run until it's time to crash."""
        self.logger.info(
            f"Crash test service starting - will crash in {self.config.run_duration}s "
            f"with exit code {self.config.exit_code}"
        )

        # Run for the configured duration
        start_time = asyncio.get_event_loop().time()

        while self.is_running:
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = self.config.run_duration - elapsed

            if remaining <= 0:
                # Time to crash!
                self.logger.error(
                    f"{self.config.crash_message} (exit code: {self.config.exit_code})"
                )
                sys.exit(self.config.exit_code)

            # Log progress
            if int(elapsed) % 2 == 0 and elapsed > int(elapsed) - 0.1:
                self.logger.info(
                    f"Service running - will crash in {remaining:.1f}s"
                )

            await asyncio.sleep(0.5)
