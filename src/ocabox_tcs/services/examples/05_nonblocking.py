"""Non-blocking permanent service example.

This example demonstrates BasePermanentService (non-blocking variant):
- Override start_service() to spawn background tasks
- Override stop_service() to clean up tasks
- Use case: Services that manage async workers or event handlers

Contrast with BaseBlockingPermanentService which uses run_service() for main loop.

Status management is automatic - no manual set_status() calls needed.

Run standalone:
    python src/ocabox_tcs/services/examples/05_nonblocking.py config/examples.yaml nonblocking

Run with launchers:
    poetry run tcs_asyncio --config config/examples.yaml
    poetry run tcs_process --config config/examples.yaml
"""
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BasePermanentService, BaseServiceConfig, config, service


@config
@dataclass
class NonBlockingConfig(BaseServiceConfig):
    """Configuration for non-blocking service."""
    worker_count: int = 3
    interval: float = 2.0


@service
class NonBlockingService(BasePermanentService):
    """Service demonstrating non-blocking permanent service pattern.

    This service spawns multiple background worker tasks that run independently.
    Useful for services that need to manage multiple concurrent operations.

    Status is managed automatically by the framework - no manual set_status() needed.
    """

    async def start_service(self):
        """Start the service by spawning background workers."""
        self.logger.info(f"Starting {self.config.worker_count} background workers")

        # Initialize state
        self.workers: list[asyncio.Task] = []
        self.stop_event = asyncio.Event()

        # Spawn worker tasks
        for i in range(self.config.worker_count):
            task = asyncio.create_task(self._worker(i))
            self.workers.append(task)

        self.logger.info(f"Service started with {self.config.worker_count} background workers")

    async def stop_service(self):
        """Stop the service and clean up all workers."""
        self.logger.info("Stopping service, cleaning up workers...")

        # Signal all workers to stop
        self.stop_event.set()

        # Wait for all workers to finish (with timeout)
        if self.workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.workers, return_exceptions=True),
                    timeout=5.0
                )
                self.logger.info("All workers stopped gracefully")
            except asyncio.TimeoutError:
                self.logger.warning("Worker cleanup timeout, cancelling remaining tasks")
                for task in self.workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*self.workers, return_exceptions=True)

        self.workers.clear()
        self.logger.info("Service cleanup complete")

    async def _worker(self, worker_id: int):
        """Background worker task.

        Args:
            worker_id: Unique identifier for this worker
        """
        self.logger.info(f"Worker {worker_id} started")

        try:
            cycle = 0
            while not self.stop_event.is_set():
                cycle += 1
                self.logger.debug(f"Worker {worker_id} cycle {cycle}")

                # Simulate work
                await asyncio.sleep(self.config.interval)

        except asyncio.CancelledError:
            self.logger.info(f"Worker {worker_id} cancelled")
            raise
        except Exception as e:
            self.logger.error(f"Worker {worker_id} error: {e}")
            # Note: In production, you might want to add error handling here
            # For example, restart the worker or set a DEGRADED status via healthcheck
        finally:
            self.logger.info(f"Worker {worker_id} stopped")


if __name__ == '__main__':
    NonBlockingService.main()
