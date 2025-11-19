"""Tests for tcsctl display functions."""

from datetime import UTC, datetime

import pytest

from ocabox_tcs.monitoring import Status
from tcsctl.client import ServiceInfo
from tcsctl.display import display_services_detailed, display_services_table


class TestServiceInfoMetadata:
    """Tests for ServiceInfo metadata display."""

    def test_service_info_with_pid_and_hostname(self):
        """Test that ServiceInfo correctly stores PID and hostname."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=12345,
            hostname="test-host.example.com",
        )

        assert service.pid == 12345
        assert service.hostname == "test-host.example.com"

    def test_service_info_without_pid_and_hostname(self):
        """Test that ServiceInfo handles missing PID and hostname gracefully."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=None,
            hostname=None,
        )

        assert service.pid is None
        assert service.hostname is None

    def test_service_info_with_all_metadata(self):
        """Test that ServiceInfo stores all metadata fields."""
        now = datetime.now(UTC)
        service = ServiceInfo(
            service_id="ocabox_tcs.services.guider:jk15",
            status=Status.OK,
            status_message="Tracking star",
            start_time=now,
            stop_time=None,
            last_heartbeat=now,
            uptime_seconds=7200,
            runner_id="runner_001",
            hostname="telescope.oca.lan",
            pid=5432,
            parent="launcher.main",
        )

        # Verify all fields are stored correctly
        assert service.service_id == "ocabox_tcs.services.guider:jk15"
        assert service.status == Status.OK
        assert service.status_message == "Tracking star"
        assert service.start_time == now
        assert service.stop_time is None
        assert service.last_heartbeat == now
        assert service.uptime_seconds == 7200
        assert service.runner_id == "runner_001"
        assert service.hostname == "telescope.oca.lan"
        assert service.pid == 5432
        assert service.parent == "launcher.main"


class TestDisplayFunctions:
    """Tests for display functions (smoke tests)."""

    def test_display_services_detailed_smoke(self, capsys):
        """Smoke test for display_services_detailed - ensure it doesn't crash."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=12345,
            hostname="test-host.example.com",
            start_time=datetime.now(UTC),
            uptime_seconds=3600,
            last_heartbeat=datetime.now(UTC),
        )

        # Should not raise an exception
        display_services_detailed([service], show_all=False, service_filter=None)

        # Capture output and verify PID and hostname are in the output
        captured = capsys.readouterr()
        assert "12345" in captured.out
        assert "test-host.example.com" in captured.out

    def test_display_services_table_smoke(self, capsys):
        """Smoke test for display_services_table - ensure it doesn't crash."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=12345,
            hostname="test-host.example.com",
            start_time=datetime.now(UTC),
            uptime_seconds=3600,
            last_heartbeat=datetime.now(UTC),
        )

        # Should not raise an exception
        display_services_table([service], show_all=False, service_filter=None)

        # Capture output and verify PID and hostname are in the output
        captured = capsys.readouterr()
        assert "12345" in captured.out
        assert "test-host.example.com" in captured.out

    def test_display_services_table_without_metadata(self, capsys):
        """Test table display gracefully handles missing PID and hostname."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=None,
            hostname=None,
            start_time=datetime.now(UTC),
            uptime_seconds=3600,
            last_heartbeat=datetime.now(UTC),
        )

        # Should not raise an exception
        display_services_table([service], show_all=False, service_filter=None)

        # Capture output and verify it doesn't crash
        captured = capsys.readouterr()
        assert "service:main" in captured.out

    def test_display_services_detailed_without_metadata(self, capsys):
        """Test detailed display gracefully handles missing PID and hostname."""
        service = ServiceInfo(
            service_id="test.service:main",
            status=Status.OK,
            pid=None,
            hostname=None,
            start_time=datetime.now(UTC),
            uptime_seconds=3600,
            last_heartbeat=datetime.now(UTC),
        )

        # Should not raise an exception
        display_services_detailed([service], show_all=False, service_filter=None)

        # Capture output and verify it doesn't crash
        captured = capsys.readouterr()
        assert "service:main" in captured.out
