"""Example external service from external package.

This service demonstrates that services can be loaded from external Python packages
by specifying the full module path in the service configuration.

Usage:

1. **Minimal (zero-config)** - Just run it:
   python tests/external_example/external_worker.py

   Uses defaults:
   - variant: "dev"
   - NATS: localhost:4222 (optional)
   - No config file needed

2. **With custom variant**:
   python tests/external_example/external_worker.py prod

3. **With config file**:
   python tests/external_example/external_worker.py config/test_external.yaml demo

4. **Via launcher** - Add to services configuration with registry:
   registry:
     external_worker: tests.external_example.external_worker
   services:
     - type: external_worker
       variant: demo
"""

import asyncio
from ocabox_tcs.base_service import service, BaseBlockingPermanentService


@service('external_worker')
class ExternalWorkerService(BaseBlockingPermanentService):
    """Example service from external package."""

    async def run_service(self):
        """Main service loop."""
        counter = 0
        while self.is_running:
            counter += 1
            self.svc_logger.info(f"External worker tick {counter}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    ExternalWorkerService.main()
