"""Example external service from external package.

This service demonstrates that services can be loaded from external Python packages
by specifying the full module path in the service configuration.

To use this service, add to services configuration:
  - type: external_worker
    instance_context: demo
    module: tests.external_example.external_worker
"""

import asyncio
from ocabox_tcs.base_service import service, BaseBlockingPermanentService


@service
class ExternalWorkerService(BaseBlockingPermanentService):
    """Example service from external package."""

    async def run_service(self):
        """Main service loop."""
        counter = 0
        while self.is_running:
            counter += 1
            self.logger.info(f"External worker tick {counter}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    ExternalWorkerService.main()
