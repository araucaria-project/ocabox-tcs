"""Mock cyclic/periodic service for testing.

A service that executes tasks periodically on a schedule.
Used for testing cyclic service lifecycle and monitoring.

Environment Variables:
    MOCK_CYCLE_INTERVAL: Interval between cycles (default: 2.0 seconds)
    MOCK_EXECUTION_DURATION: Duration of each cycle (default: 0.5 seconds)
    MOCK_MAX_CYCLES: Maximum cycles before stopping (0 = infinite, default: 0)
    MOCK_FAIL_ON_CYCLE: Cycle number to fail on (0 = never, default: 0)
"""

import asyncio
import logging
import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import BasePermanentService, config, service
from ocabox_tcs.monitoring import Status

logger = logging.getLogger(__name__)


@config('mock_cyclic')
@dataclass
class MockCyclicConfig:
    """Configuration for mock cyclic service."""
    cycle_interval: float = 2.0  # Interval between cycles (seconds)
    execution_duration: float = 0.5  # Duration of each cycle execution
    max_cycles: int = 0  # Maximum cycles (0 = infinite)
    fail_on_cycle: int = 0  # Cycle number to fail on (0 = never)


@service('mock_cyclic')
class MockCyclicService(BasePermanentService):
    """Mock cyclic/periodic service for testing.

    Runs tasks periodically on a schedule. Used for testing:
    - Cyclic/periodic service patterns
    - Schedule management
    - Cycle completion tracking
    - Monitoring for periodic tasks
    """

    config: MockCyclicConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cycle_count = 0
        self.last_cycle_time = None
        self.cycle_task: asyncio.Task | None = None

    async def start_service(self):
        """Start cyclic service."""
        self.svc_logger.info("Starting cyclic service")
        # Start background cycle task
        self.cycle_task = asyncio.create_task(self._cycle_loop())

    async def stop_service(self):
        """Stop cyclic service."""
        self.svc_logger.info("Stopping cyclic service")
        # Cancel cycle task
        if self.cycle_task:
            self.cycle_task.cancel()
            try:
                await self.cycle_task
            except asyncio.CancelledError:
                pass
        self.svc_logger.info(f"Stopped after {self.cycle_count} cycles")

    async def _cycle_loop(self):
        """Main cycle loop - runs tasks periodically."""
        try:
            while self.is_running:
                # Wait for next cycle - exit-aware sleep
                if not await self.sleep(self.svc_config.cycle_interval):
                    self.svc_logger.info("Stop signal received during sleep")
                    break

                # Execute cycle
                try:
                    await self._execute_cycle()
                except Exception as e:
                    self.svc_logger.error(f"Cycle {self.cycle_count} failed: {e}")
                    self.monitor.set_status(Status.ERROR, f"Cycle failed: {e}")

                    # Check if this was an intentional failure
                    if self.svc_config.fail_on_cycle > 0 and self.cycle_count == self.svc_config.fail_on_cycle:
                        raise  # Re-raise to propagate failure

                # Check if we've reached max cycles
                if self.svc_config.max_cycles > 0 and self.cycle_count >= self.svc_config.max_cycles:
                    self.svc_logger.info(f"Reached max cycles ({self.svc_config.max_cycles}), stopping")
                    break

        except asyncio.CancelledError:
            self.svc_logger.debug("Cycle loop cancelled")
            raise
        except Exception as e:
            self.svc_logger.error(f"Fatal error in cycle loop: {e}")
            raise

    async def _execute_cycle(self):
        """Execute a single cycle."""
        import time

        self.cycle_count += 1
        cycle_start = time.time()

        self.svc_logger.info(f"Starting cycle {self.cycle_count}")

        # Check if this cycle should fail
        if self.svc_config.fail_on_cycle > 0 and self.cycle_count == self.svc_config.fail_on_cycle:
            self.svc_logger.error(f"Simulated failure on cycle {self.cycle_count}")
            raise RuntimeError(f"Simulated failure on cycle {self.cycle_count}")

        # Simulate work - exit-aware sleep
        await self.sleep(self.svc_config.execution_duration)

        cycle_duration = time.time() - cycle_start
        self.last_cycle_time = cycle_start

        self.svc_logger.info(
            f"Completed cycle {self.cycle_count} "
            f"(duration: {cycle_duration:.2f}s)"
        )

        # Update status to reflect cycle completion
        self.monitor.set_status(
            Status.OK,
            f"Cycle {self.cycle_count} completed"
        )

    def healthcheck(self) -> Status:
        """Custom healthcheck based on cycle status."""
        # Check if cycles are running as expected
        if self.cycle_count == 0 and self.is_running:
            # Service is running but no cycles completed yet
            return Status.STARTUP

        return Status.OK


if __name__ == '__main__':
    # Use base class's main() - now supports external modules (Feature #7)
    MockCyclicService.main()
