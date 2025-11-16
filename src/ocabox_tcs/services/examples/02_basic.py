"""Basic service with custom configuration.

This example shows how to add configuration to your service using dataclasses.
The @config decorator registers the config class with the framework.

Run standalone:
    python src/ocabox_tcs/services/examples/02_basic.py config/examples.yaml basic

Run with launchers:
    poetry run tcs_asyncio --config config/examples.yaml
    poetry run tcs_process --config config/examples.yaml
"""
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service


@config
@dataclass
class BasicConfig(BaseServiceConfig):
    """Configuration for basic service."""
    interval: float = 3.0
    message: str = "Hello from basic service"


@service
class BasicService(BaseBlockingPermanentService):
    """Service demonstrating configuration usage."""

    async def run_service(self):
        """Main service loop using configuration."""
        while self.is_running:
            self.svc_logger.info(f"{self.svc_config.message} (every {self.svc_config.interval}s)")
            await asyncio.sleep(self.svc_config.interval)


if __name__ == '__main__':
    BasicService.main()
