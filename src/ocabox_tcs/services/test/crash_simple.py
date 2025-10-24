"""Simple test service that exits immediately with configured code.

This is for testing restart policies with specific exit codes.
"""

import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import service, config, BaseSingleShotService, BaseServiceConfig


@config
@dataclass
class TestCrashSimpleConfig(BaseServiceConfig):
    """Configuration for simple crash test service."""
    exit_code: int = 1  # Exit code (0=success, 1=failure, >128=abnormal)


@service
class TestCrashSimpleService(BaseSingleShotService):
    """Service that exits immediately with configured code."""

    config: TestCrashSimpleConfig

    async def execute(self) -> None:
        """Exit immediately with configured code."""
        self.logger.info(f"Exiting with code {self.config.exit_code}")
        sys.exit(self.config.exit_code)
