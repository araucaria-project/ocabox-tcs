"""Service with monitoring and health checks.

This example demonstrates the monitoring framework:
- Status reporting (OK, DEGRADED, ERROR, FAILED)
- Health check callbacks
- Error tracking and recovery
- Automatic shutdown on critical failures

Run standalone:
    python src/ocabox_tcs/services/examples/04_monitoring.py config/examples.yaml monitoring

Run with launchers:
    poetry run tcs_asyncio --config config/examples.yaml
    poetry run tcs_process --config config/examples.yaml
"""
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service
from ocabox_tcs.monitoring import Status


@config
@dataclass
class MonitoringConfig(BaseServiceConfig):
    """Configuration for monitoring service."""
    interval: float = 2.0
    max_errors: int = 3  # Fail after this many consecutive errors


@service
class MonitoringService(BaseBlockingPermanentService):
    """Service demonstrating monitoring and health checks."""

    async def on_start(self):
        """Initialize before main loop."""
        self.error_count = 0
        self.cycle_count = 0

        # Register health check callback
        self.monitor.add_healthcheck_cb(self.healthcheck)
        self.monitor.set_status(Status.OK, "Service started")

        self.logger.info("Monitoring service ready")

    async def run_service(self):
        """Main loop with error handling and status reporting."""
        while self.is_running:
            try:
                self.cycle_count += 1

                # Simulate occasional errors (every 10th cycle)
                if self.cycle_count % 10 == 0:
                    raise ValueError("Simulated error")

                # Reset error count on success
                self.error_count = 0
                self.monitor.set_status(Status.OK, f"Cycle {self.cycle_count}")
                self.logger.info(f"Cycle {self.cycle_count} completed")

                await asyncio.sleep(self.config.interval)

            except asyncio.CancelledError:
                # Normal shutdown
                break
            except Exception as e:
                self.error_count += 1
                self.logger.error(f"Error in cycle {self.cycle_count}: {e}")

                # Update status based on error count
                if self.error_count >= self.config.max_errors:
                    self.monitor.set_status(Status.FAILED, "Max errors exceeded")
                    self.logger.critical("Too many errors, shutting down")
                    break
                else:
                    self.monitor.set_status(Status.ERROR, f"Error count: {self.error_count}")

                await asyncio.sleep(self.config.interval)

    def healthcheck(self) -> Status:
        """Health check callback - return current health status.

        This is called periodically by the monitoring framework to check
        if the service is healthy.
        """
        if self.error_count >= self.config.max_errors:
            return Status.FAILED
        elif self.error_count > 0:
            return Status.DEGRADED
        return Status.OK

    async def on_stop(self):
        """Cleanup after main loop stops."""
        self.monitor.set_status(Status.SHUTDOWN, "Service stopping")
        self.logger.info(f"Service stopped after {self.cycle_count} cycles")


if __name__ == '__main__':
    MonitoringService.main()
