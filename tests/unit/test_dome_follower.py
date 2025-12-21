"""Unit tests for dome_follower service.

Tests configuration, initialization, and basic functionality of the dome follower.
"""

import asyncio
import pytest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from ocabox_tcs.services.dome_follower_svc.dome_follower import (
    DomeFollowerService,
    DomeFollowerServiceConfig,
)
from ocabox_tcs.services.dome_follower_svc.manager import Manager
from ocabox_tcs.monitoring import Status


class TestDomeFollowerConfiguration:
    """Test dome follower configuration."""

    def test_config_defaults(self):
        """Test default configuration values."""
        config = DomeFollowerServiceConfig(type="dome_follower", variant="jk15")

        assert config.interval == 1.0
        assert config.turn_on_automatically is False
        assert config.dome_speed == 30
        assert config.follow_tolerance == 3.0
        assert config.settle_time == 3.0

    def test_config_custom_values(self):
        """Test custom configuration values."""
        config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="wk06",
            interval=2.0,
            turn_on_automatically=True,
            dome_speed=25.0,
            follow_tolerance=5.0,
            settle_time=2.5,
        )

        assert config.type == "dome_follower"
        assert config.variant == "wk06"
        assert config.interval == 2.0
        assert config.turn_on_automatically is True
        assert config.dome_speed == 25.0
        assert config.follow_tolerance == 5.0
        assert config.settle_time == 2.5

    def test_config_service_id(self):
        """Test service_id property."""
        config = DomeFollowerServiceConfig(type="dome_follower", variant="zb08")

        assert config.id == "dome_follower.zb08"


class TestManagerInitialization:
    """Test Manager class initialization and basic functionality."""

    @pytest.fixture
    def service_mock(self):
        """Create a mock DomeFollowerService."""
        service = MagicMock(spec=DomeFollowerService)
        service.svc_logger = MagicMock()
        service.svc_config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
            interval=1.0,
            dome_speed=30.0,
            follow_tolerance=3.0,
            settle_time=3.0,
        )
        service.monitor = MagicMock()
        service.monitor.track_task = MagicMock()
        service.is_running = True
        return service

    def test_manager_init(self, service_mock):
        """Test Manager initialization with service and config."""
        manager = Manager(service=service_mock, config=service_mock.svc_config)

        assert manager.service == service_mock
        assert manager.config == service_mock.svc_config
        assert manager.svc_logger == service_mock.svc_logger
        assert manager.follow_on is False
        assert manager.client_name == "CliClient"
        assert manager.software_id == "dome_follower"

    def test_manager_config_access(self, service_mock):
        """Test Manager can access config attributes."""
        config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
            dome_speed=25.0,
            follow_tolerance=5.0,
            settle_time=2.0,
        )
        manager = Manager(service=service_mock, config=config)

        # Should be able to access config attributes
        assert manager.config.variant == "jk15"
        assert manager.config.dome_speed == 25.0
        assert manager.config.follow_tolerance == 5.0
        assert manager.config.settle_time == 2.0

    def test_manager_logger_access(self, service_mock):
        """Test Manager can access service logger."""
        manager = Manager(service=service_mock, config=service_mock.svc_config)

        # Should not raise AttributeError
        manager.svc_logger.info("Test log message")

        # Verify logger was called
        manager.svc_logger.info.assert_called_once_with("Test log message")

    @pytest.mark.asyncio
    async def test_set_follow_parameters(self, service_mock):
        """Test follow parameters are set from config."""
        config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
            follow_tolerance=5.0,
            settle_time=2.0,
            dome_speed=25.0,
        )
        manager = Manager(service=service_mock, config=config)

        await manager.set_follow_parameters()

        assert manager.follow_tolerance == 5.0
        assert manager.settle_time == 2.0
        assert manager.dome_speed_deg == 25.0

        # Verify logger was called
        manager.svc_logger.info.assert_called()

    @pytest.mark.asyncio
    async def test_dome_slew_settle_with_speed(self, service_mock):
        """Test dome slew settle calculation with speed."""
        manager = Manager(service=service_mock, config=service_mock.svc_config)
        manager.dome_speed_deg = 30.0  # deg/s

        # Mock sleep to avoid actual delays
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await manager.dome_slew_settle(angle_to_go=60.0)

            # Should sleep for 60 deg / 30 deg/s = 2.0 seconds
            mock_sleep.assert_called_once_with(2.0)

    @pytest.mark.asyncio
    async def test_dome_slew_settle_without_speed(self, service_mock):
        """Test dome slew settle uses default settle_time when speed not available."""
        manager = Manager(service=service_mock, config=service_mock.svc_config)
        manager.dome_speed_deg = None
        manager.settle_time = 3.0

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await manager.dome_slew_settle(angle_to_go=60.0)

            # Should sleep for default settle_time
            mock_sleep.assert_called_once_with(3.0)


class TestDomeFollowerService:
    """Test DomeFollowerService class."""

    @pytest.fixture
    def service_instance(self):
        """Create a service instance with mocked dependencies."""
        service = DomeFollowerService()

        # Mock framework-provided attributes
        service.svc_logger = MagicMock()
        service.svc_config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
            interval=1.0,
            turn_on_automatically=False,
        )
        service.monitor = MagicMock()
        service.controller = MagicMock()
        service.is_running = True

        return service

    def test_service_initialization(self, service_instance):
        """Test service initializes with None manager."""
        assert service_instance.manager is None

    @pytest.mark.asyncio
    async def test_on_start_creates_manager(self, service_instance):
        """Test on_start creates Manager instance."""
        # Mock Manager methods to avoid actual NATS/TIC connections
        with patch.object(Manager, "start_comm", new_callable=AsyncMock) as mock_start_comm:
            with patch.object(
                Manager, "set_follow_parameters", new_callable=AsyncMock
            ) as mock_set_params:
                await service_instance.on_start()

                # Verify manager was created
                assert service_instance.manager is not None
                assert isinstance(service_instance.manager, Manager)

                # Verify manager methods were called
                mock_start_comm.assert_called_once()
                mock_set_params.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_start_with_auto_turn_on(self, service_instance):
        """Test on_start enables follow when turn_on_automatically is True."""
        service_instance.svc_config.turn_on_automatically = True

        with patch.object(Manager, "start_comm", new_callable=AsyncMock):
            with patch.object(Manager, "set_follow_parameters", new_callable=AsyncMock):
                await service_instance.on_start()

                # Verify follow_on was enabled
                assert service_instance.manager.follow_on is True

                # Verify warning was logged
                service_instance.svc_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_start_without_auto_turn_on(self, service_instance):
        """Test on_start doesn't enable follow when turn_on_automatically is False."""
        service_instance.svc_config.turn_on_automatically = False

        with patch.object(Manager, "start_comm", new_callable=AsyncMock):
            with patch.object(Manager, "set_follow_parameters", new_callable=AsyncMock):
                await service_instance.on_start()

                # Verify follow_on remains False
                assert service_instance.manager.follow_on is False

    @pytest.mark.asyncio
    async def test_run_service_loop(self, service_instance):
        """Test run_service calls dome_follow in loop."""
        # Create a real manager (with mocked comm methods)
        with patch.object(Manager, "start_comm", new_callable=AsyncMock):
            with patch.object(Manager, "set_follow_parameters", new_callable=AsyncMock):
                await service_instance.on_start()

        # Mock dome_follow
        dome_follow_called = 0

        async def mock_dome_follow():
            nonlocal dome_follow_called
            dome_follow_called += 1
            # Stop after 3 iterations
            if dome_follow_called >= 3:
                service_instance.is_running = False

        service_instance.manager.dome_follow = mock_dome_follow

        # Mock sleep to speed up test
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await service_instance.run_service()

        # Verify dome_follow was called 3 times
        assert dome_follow_called == 3

    @pytest.mark.asyncio
    async def test_run_service_handles_cancellation(self, service_instance):
        """Test run_service handles asyncio.CancelledError gracefully."""
        with patch.object(Manager, "start_comm", new_callable=AsyncMock):
            with patch.object(Manager, "set_follow_parameters", new_callable=AsyncMock):
                await service_instance.on_start()

        # Mock dome_follow to raise CancelledError
        async def mock_dome_follow():
            raise asyncio.CancelledError()

        service_instance.manager.dome_follow = mock_dome_follow

        # Should not raise exception
        await service_instance.run_service()

    @pytest.mark.asyncio
    async def test_on_stop_calls_stop_comm(self, service_instance):
        """Test on_stop calls manager.stop_comm."""
        # Initialize manager
        with patch.object(Manager, "start_comm", new_callable=AsyncMock):
            with patch.object(Manager, "set_follow_parameters", new_callable=AsyncMock):
                await service_instance.on_start()

        # Mock stop_comm
        service_instance.manager.stop_comm = AsyncMock()

        await service_instance.on_stop()

        # Verify stop_comm was called
        service_instance.manager.stop_comm.assert_called_once()


class TestDomeFollowLogic:
    """Test dome following logic in Manager."""

    @pytest.fixture
    def manager_with_mocks(self):
        """Create Manager with mocked TIC connection."""
        service = MagicMock(spec=DomeFollowerService)
        service.svc_logger = MagicMock()
        service.svc_config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
            dome_speed=30.0,
            follow_tolerance=3.0,
            settle_time=3.0,
        )
        service.monitor = MagicMock()
        service.monitor.track_task = MagicMock()
        service.monitor.set_status = MagicMock()
        service.monitor.cancel_error_status = MagicMock()
        service.is_running = True

        # Mock context manager for track_task
        track_task_cm = MagicMock()
        track_task_cm.__aenter__ = AsyncMock(return_value=None)
        track_task_cm.__aexit__ = AsyncMock(return_value=None)
        service.monitor.track_task.return_value = track_task_cm

        manager = Manager(service=service, config=service.svc_config)

        # Mock TIC connection
        manager.tic_conn = MagicMock()
        manager.tic_conn.dome = MagicMock()
        manager.tic_conn.mount = MagicMock()

        # Set initial follow parameters
        manager.follow_tolerance = 3.0
        manager.settle_time = 3.0
        manager.dome_speed_deg = 30.0
        manager.dome_az_last = None
        manager.dome_current_speed = 0.0

        return manager

    @pytest.mark.asyncio
    async def test_dome_follow_when_disabled(self, manager_with_mocks):
        """Test dome_follow does nothing when follow_on is False."""
        manager_with_mocks.follow_on = False

        await manager_with_mocks.dome_follow()

        # TIC should not be queried
        manager_with_mocks.tic_conn.dome.aget_slewing.assert_not_called()

    @pytest.mark.asyncio
    async def test_dome_follow_when_enabled_no_slew_needed(self, manager_with_mocks):
        """Test dome_follow when dome and mount are aligned."""
        manager_with_mocks.follow_on = True

        # Mock TIC responses - dome and mount aligned
        manager_with_mocks.tic_conn.dome.aget_slewing = AsyncMock(return_value=False)
        manager_with_mocks.tic_conn.dome.aget_az = AsyncMock(return_value=180.0)
        manager_with_mocks.tic_conn.mount.aget_az = AsyncMock(return_value=181.0)  # Within tolerance
        manager_with_mocks.tic_conn.mount.aget_slewing = AsyncMock(return_value=False)

        await manager_with_mocks.dome_follow()

        # Should query positions but not slew (within 3 deg tolerance)
        manager_with_mocks.tic_conn.dome.aget_slewing.assert_called_once()
        manager_with_mocks.tic_conn.dome.aget_az.assert_called_once()
        manager_with_mocks.tic_conn.mount.aget_az.assert_called_once()
        manager_with_mocks.tic_conn.mount.aget_slewing.assert_called_once()
        manager_with_mocks.tic_conn.dome.aput_slewtoazimuth.assert_not_called()

    @pytest.mark.asyncio
    async def test_dome_follow_slews_when_out_of_tolerance(self, manager_with_mocks):
        """Test dome_follow triggers slew when out of tolerance."""
        manager_with_mocks.follow_on = True

        # Mock TIC responses - dome and mount misaligned
        manager_with_mocks.tic_conn.dome.aget_slewing = AsyncMock(return_value=False)
        manager_with_mocks.tic_conn.dome.aget_az = AsyncMock(return_value=180.0)
        manager_with_mocks.tic_conn.mount.aget_az = AsyncMock(return_value=190.0)  # 10 deg off
        manager_with_mocks.tic_conn.mount.aget_slewing = AsyncMock(return_value=False)
        manager_with_mocks.tic_conn.dome.aput_slewtoazimuth = AsyncMock()

        # Mock dome_slew_settle to avoid delays
        manager_with_mocks.dome_slew_settle = AsyncMock()

        await manager_with_mocks.dome_follow()

        # Should slew to mount position
        manager_with_mocks.tic_conn.dome.aput_slewtoazimuth.assert_called_once_with(190.0)
        manager_with_mocks.dome_slew_settle.assert_called_once()

    @pytest.mark.asyncio
    async def test_dome_follow_skips_when_dome_slewing(self, manager_with_mocks):
        """Test dome_follow doesn't trigger new slew when dome already slewing."""
        manager_with_mocks.follow_on = True

        # Mock TIC responses - dome is slewing
        manager_with_mocks.tic_conn.dome.aget_slewing = AsyncMock(return_value=True)
        manager_with_mocks.tic_conn.dome.aget_az = AsyncMock(return_value=180.0)
        manager_with_mocks.tic_conn.mount.aget_az = AsyncMock(return_value=190.0)
        manager_with_mocks.tic_conn.mount.aget_slewing = AsyncMock(return_value=False)

        await manager_with_mocks.dome_follow()

        # Should not trigger slew
        manager_with_mocks.tic_conn.dome.aput_slewtoazimuth.assert_not_called()

    @pytest.mark.asyncio
    async def test_dome_follow_skips_when_mount_slewing(self, manager_with_mocks):
        """Test dome_follow doesn't trigger slew when mount is slewing."""
        manager_with_mocks.follow_on = True

        # Mock TIC responses - mount is slewing
        manager_with_mocks.tic_conn.dome.aget_slewing = AsyncMock(return_value=False)
        manager_with_mocks.tic_conn.dome.aget_az = AsyncMock(return_value=180.0)
        manager_with_mocks.tic_conn.mount.aget_az = AsyncMock(return_value=190.0)
        manager_with_mocks.tic_conn.mount.aget_slewing = AsyncMock(return_value=True)

        await manager_with_mocks.dome_follow()

        # Should not trigger slew
        manager_with_mocks.tic_conn.dome.aput_slewtoazimuth.assert_not_called()


class TestErrorHandling:
    """Test error handling in dome follower."""

    @pytest.fixture
    def manager_with_errors(self):
        """Create Manager that can simulate TIC errors."""
        service = MagicMock(spec=DomeFollowerService)
        service.svc_logger = MagicMock()
        service.svc_config = DomeFollowerServiceConfig(
            type="dome_follower",
            variant="jk15",
        )
        service.monitor = MagicMock()
        service.monitor.track_task = MagicMock()
        service.monitor.set_status = MagicMock()
        service.is_running = True

        # Mock context manager for track_task
        track_task_cm = MagicMock()
        track_task_cm.__aenter__ = AsyncMock(return_value=None)
        track_task_cm.__aexit__ = AsyncMock(return_value=None)
        service.monitor.track_task.return_value = track_task_cm

        manager = Manager(service=service, config=service.svc_config)
        manager.follow_on = True

        # Mock TIC connection
        manager.tic_conn = MagicMock()
        manager.tic_conn.dome = MagicMock()
        manager.tic_conn.mount = MagicMock()

        manager.follow_tolerance = 3.0
        manager.dome_current_speed = 0.0

        return manager

    @pytest.mark.asyncio
    async def test_handles_server_error(self, manager_with_errors):
        """Test dome_follow handles OcaboxServerError."""
        from ocaboxapi.exceptions import OcaboxServerError

        # Mock TIC to raise error
        manager_with_errors.tic_conn.dome.aget_slewing = AsyncMock(
            side_effect=OcaboxServerError("Server error")
        )

        await manager_with_errors.dome_follow()

        # Should log error and set ERROR status
        manager_with_errors.svc_logger.error.assert_called()
        manager_with_errors.service.monitor.set_status.assert_called_with(
            Status.ERROR, "Tic dome get Server Error Server error"
        )

    @pytest.mark.asyncio
    async def test_handles_timeout_error(self, manager_with_errors):
        """Test dome_follow handles CommunicationTimeoutError."""
        from obcom.comunication.comunication_error import CommunicationTimeoutError

        # Mock TIC to raise timeout
        manager_with_errors.tic_conn.dome.aget_slewing = AsyncMock(
            side_effect=CommunicationTimeoutError()
        )

        await manager_with_errors.dome_follow()

        # Should log error and set DEGRADED status
        manager_with_errors.svc_logger.error.assert_called()
        manager_with_errors.service.monitor.set_status.assert_called_with(
            Status.DEGRADED, "Tic dome get Time out"
        )

    @pytest.mark.asyncio
    async def test_handles_access_denied_error(self, manager_with_errors):
        """Test dome_follow handles OcaboxAccessDenied."""
        from ocaboxapi.exceptions import OcaboxAccessDenied

        # Mock TIC to raise access denied
        manager_with_errors.tic_conn.dome.aget_slewing = AsyncMock(
            side_effect=OcaboxAccessDenied()
        )

        await manager_with_errors.dome_follow()

        # Should log error and set ERROR status
        manager_with_errors.svc_logger.error.assert_called()
        manager_with_errors.service.monitor.set_status.assert_called_with(
            Status.ERROR, "Tic dome get Access Denied"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
