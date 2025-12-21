"""Simple test service that exits immediately with configured code.

This is for testing restart policies with specific exit codes.
"""

import sys
from dataclasses import dataclass

from ocabox_tcs.base_service import service, config, BaseSingleShotService, BaseServiceConfig


@config('test.crash_simple')
@dataclass
class TestCrashSimpleConfig(BaseServiceConfig):
    """Configuration for simple crash test service."""
    exit_code: int = 1  # Exit code (0=success, 1=failure, >128=abnormal)


@service('test.crash_simple')
class TestCrashSimpleService(BaseSingleShotService):
    """Service that exits immediately with configured code."""

    config: TestCrashSimpleConfig

    async def execute(self) -> None:
        """Exit immediately with configured code."""
        self.svc_logger.info(f"Exiting with code {self.svc_config.exit_code}")
        sys.exit(self.svc_config.exit_code)
