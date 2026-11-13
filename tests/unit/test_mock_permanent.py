"""Regression tests for the permanent-service test fixture."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from tests.services.mock_permanent import MockPermanentConfig, MockPermanentService


@pytest.mark.asyncio
@pytest.mark.parametrize("sleep_results", [[False], [True, False]])
async def test_mock_permanent_exits_when_sleep_is_interrupted(sleep_results):
    service = MockPermanentService()
    service.svc_config = MockPermanentConfig()
    service.svc_logger = logging.getLogger("test")
    service.controller = Mock(is_running=True)
    service.controller.sleep = AsyncMock(
        side_effect=[*sleep_results, AssertionError("Worker ignored the stop signal")]
    )

    await service.run_service()

    assert service.iteration_count == len(sleep_results)
    assert service.controller.sleep.await_count == len(sleep_results)
