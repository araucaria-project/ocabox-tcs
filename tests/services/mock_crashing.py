"""Mock crashing service for testing.

A service that deliberately crashes with configurable behavior.
Used for testing restart policies, crash handling, and monitoring.

Environment Variables:
    MOCK_CRASH_DELAY: Delay before crash in seconds (default: 0.5)
    MOCK_EXIT_CODE: Exit code to use when crashing (default: 1)
    MOCK_CRASH_ON_ITERATION: Crash on specific iteration (default: 1)
    MOCK_CRASH_TYPE: Type of crash (exit/exception/signal) (default: exit)
    MOCK_SIGNAL: Signal number for signal crash (default: 15 = SIGTERM)
"""

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, config, service

logger = logging.getLogger(__name__)


@dataclass
@config
class MockCrashingConfig:
    """Configuration for mock crashing service."""
    crash_delay: float = 0.5  # Delay before crash (seconds)
    exit_code: int = 1  # Exit code for crash
    crash_on_iteration: int = 1  # Which iteration to crash on
    crash_type: str = "exit"  # exit, exception, or signal
    signal_number: int = signal.SIGTERM  # Signal to use for signal crash


@service
class MockCrashingService(BaseBlockingPermanentService):
    """Mock service that deliberately crashes.

    Used for testing:
    - Restart policies (no, always, on-failure, on-abnormal)
    - Restart limits (max restarts, restart window)
    - Status transitions (startup -> error/failed)
    - Crash event publishing
    - Launcher crash handling
    """

    config: MockCrashingConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iteration_count = 0

    async def on_start(self):
        """Called before run_service starts."""
        self.logger.info(f"MockCrashingService starting (will crash soon)")
        self.logger.info(f"Config: crash_delay={self.config.crash_delay}, exit_code={self.config.exit_code}, "
                        f"crash_on_iteration={self.config.crash_on_iteration}, crash_type={self.config.crash_type}")

    async def on_stop(self):
        """Called after run_service stops."""
        self.logger.info("MockCrashingService stopped")

    async def run_service(self):
        """Main service loop - crashes after configured delay/iteration."""
        self.logger.info("run_service() called - entering main loop")
        self.logger.info(f"is_running={self.is_running}")

        while self.is_running:
            self.iteration_count += 1
            self.logger.info(f"Iteration {self.iteration_count}")

            # Check if we should crash this iteration
            if self.iteration_count >= self.config.crash_on_iteration:
                self.logger.warning(
                    f"Crashing now (type={self.config.crash_type}, "
                    f"exit_code={self.config.exit_code})"
                )
                await asyncio.sleep(self.config.crash_delay)
                self._trigger_crash()
                break

            await asyncio.sleep(0.1)

        self.logger.info("run_service() loop exited")

    def _trigger_crash(self):
        """Trigger configured crash type."""
        crash_type = self.config.crash_type.lower()

        if crash_type == "exit":
            # Clean exit with non-zero code
            self.logger.error(f"Exiting with code {self.config.exit_code}")
            sys.exit(self.config.exit_code)

        elif crash_type == "exception":
            # Unhandled exception
            self.logger.error("Raising unhandled exception")
            raise RuntimeError(f"Mock crash exception (exit_code={self.config.exit_code})")

        elif crash_type == "signal":
            # Send signal to self
            sig = self.config.signal_number
            self.logger.error(f"Sending signal {sig} to self")
            os.kill(os.getpid(), sig)

        else:
            self.logger.error(f"Unknown crash type: {crash_type}, using exit")
            sys.exit(self.config.exit_code)


if __name__ == '__main__':
    # Use base class's main() - now supports external modules (Feature #7)
    MockCrashingService.main()
