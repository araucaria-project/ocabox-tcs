"""Mock single-shot service for testing.

A service that executes once and terminates.
Used for testing single-shot service lifecycle and monitoring.

Environment Variables:
    MOCK_EXECUTION_DELAY: Delay during execution (default: 1.0 seconds)
    MOCK_EXIT_CODE: Exit code after execution (default: 0)
    MOCK_SHOULD_FAIL: Set to "true" to simulate failure (default: false)
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseSingleShotService, config, service
from ocabox_tcs.monitoring import Status

logger = logging.getLogger(__name__)


@dataclass
@config
class MockSingleShotConfig:
    """Configuration for mock single-shot service."""
    execution_delay: float = 1.0  # Delay during execution
    exit_code: int = 0  # Exit code after execution
    should_fail: bool = False  # Simulate failure if True
    work_iterations: int = 3  # Number of work iterations


@service
class MockSingleShotService(BaseSingleShotService):
    """Mock single-shot service for testing.

    Executes once and terminates. Used for testing:
    - Single-shot service lifecycle
    - Execution completion
    - Success/failure scenarios
    - Monitoring integration for non-permanent services
    """

    config: MockSingleShotConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def execute(self):
        """Execute single-shot task.

        This method is called once when the service starts.
        After completion, the service terminates.
        """
        self.logger.info("Starting single-shot execution")

        # Simulate work with multiple iterations
        for i in range(1, self.config.work_iterations + 1):
            self.logger.info(f"Work iteration {i}/{self.config.work_iterations}")
            await asyncio.sleep(self.config.execution_delay / self.config.work_iterations)

            # Check for failure condition mid-execution
            if self.config.should_fail and i == self.config.work_iterations // 2:
                self.logger.error("Simulated failure during execution")
                self.monitor.set_status(Status.ERROR, "Execution failed")
                raise RuntimeError("Simulated execution failure")

        # Successful completion
        self.logger.info("Single-shot execution completed successfully")

        # Exit with configured exit code if non-zero
        if self.config.exit_code != 0:
            self.logger.warning(f"Exiting with code {self.config.exit_code}")
            sys.exit(self.config.exit_code)


if __name__ == '__main__':
    # Use base class's main() - now supports external modules (Feature #7)
    MockSingleShotService.main()
