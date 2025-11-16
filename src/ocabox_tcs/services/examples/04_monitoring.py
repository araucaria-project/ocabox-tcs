"""Service with monitoring and health checks.

This example demonstrates MANUAL status control for advanced use cases:
- Status reporting (OK, DEGRADED, ERROR, FAILED)
- Health check callbacks for automatic status
- Manual status overrides for specific conditions
- Error tracking and recovery

IMPORTANT: Most services don't need manual status control!
The framework automatically manages STARTUP/OK/SHUTDOWN transitions.
Use manual control only when you need fine-grained status reporting based on
internal state (like this error counter example).

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
    """Service demonstrating ADVANCED monitoring with manual status control.

    This example shows TWO approaches to status management:
    1. Healthcheck callback (recommended) - monitoring loop auto-updates status
    2. Manual set_status() calls - for immediate status changes

    Most services only need healthcheck callbacks!
    """

    async def on_start(self):
        """Initialize before main loop."""
        self.error_count = 0
        self.cycle_count = 0

        # Register health check callback - monitoring loop will call this periodically
        # and automatically update status based on return value
        self.monitor.add_healthcheck_cb(self.healthcheck)

        # Manual status override - usually not needed, but shown for demonstration
        # The controller already set status to OK after start_service() completed
        self.monitor.set_status(Status.OK, "Service started and monitoring configured")

        self.svc_logger.info("Monitoring service ready")

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

                # Manual status update - NOTE: Not necessary! The healthcheck callback
                # below will automatically report OK status. This is just for demonstration.
                self.monitor.set_status(Status.OK, f"Cycle {self.cycle_count}")
                self.svc_logger.info(f"Cycle {self.cycle_count} completed")

                await asyncio.sleep(self.svc_config.interval)

            except asyncio.CancelledError:
                # Normal shutdown
                break
            except Exception as e:
                self.error_count += 1
                self.svc_logger.error(f"Error in cycle {self.cycle_count}: {e}")

                # Manual status update on error
                # This is one case where manual updates make sense - immediate feedback
                # rather than waiting for next healthcheck cycle
                if self.error_count >= self.svc_config.max_errors:
                    self.monitor.set_status(Status.FAILED, "Max errors exceeded")
                    self.svc_logger.critical("Too many errors, shutting down")
                    break
                else:
                    self.monitor.set_status(Status.ERROR, f"Error count: {self.error_count}")

                await asyncio.sleep(self.svc_config.interval)

    def healthcheck(self) -> Status:
        """Health check callback - return current health status.

        RECOMMENDED APPROACH: Return status based on internal state.
        The monitoring loop calls this periodically and automatically updates
        the service status. No manual set_status() needed!

        This is the preferred way to manage status for most services.
        """
        if self.error_count >= self.svc_config.max_errors:
            return Status.FAILED
        elif self.error_count > 0:
            return Status.DEGRADED
        return Status.OK

    async def on_stop(self):
        """Cleanup after main loop stops."""
        # Manual status update - NOTE: Not necessary! Controller automatically
        # sets SHUTDOWN status during stop_service(). Shown for demonstration.
        self.monitor.set_status(Status.SHUTDOWN, "Service stopping")
        self.svc_logger.info(f"Service stopped after {self.cycle_count} cycles")


if __name__ == '__main__':
    MonitoringService.main()
