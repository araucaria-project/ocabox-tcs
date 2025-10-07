"""Minimal service example - absolute bare minimum.

This is the simplest possible TCS service - just prints a message every 5 seconds.
Perfect starting point for understanding the framework.

Run standalone:
    python src/ocabox_tcs/services/examples/01_minimal.py config/examples.yaml minimal

Run with asyncio launcher:
    poetry run tcs_asyncio --config config/examples.yaml

Run with process launcher:
    poetry run tcs_process --config config/examples.yaml
"""
import asyncio

from ocabox_tcs.base_service import BaseBlockingPermanentService, service


@service
class MinimalService(BaseBlockingPermanentService):
    """Simplest service - just prints a message every 5 seconds."""

    async def run_service(self):
        """Main service loop."""
        while self.is_running:
            self.logger.info("Service running...")
            await asyncio.sleep(5)


if __name__ == '__main__':
    MinimalService.main()
