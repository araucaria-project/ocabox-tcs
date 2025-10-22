"""Tests for monitoring system."""
import asyncio
from unittest.mock import Mock, AsyncMock, patch

import pytest

from ocabox_tcs.monitoring import create_monitor, Status
from ocabox_tcs.monitoring.monitored_object import (
    MonitoredObject,
    DummyMonitoredObject,
    ReportingMonitoredObject,
)
from ocabox_tcs.monitoring.status import aggregate_status, StatusReport


class TestStatus:
    """Tests for Status enum."""

    def test_status_values(self):
        """Test that all expected status values exist."""
        assert Status.UNKNOWN.value == "unknown"
        assert Status.STARTUP.value == "startup"
        assert Status.OK.value == "ok"
        assert Status.IDLE.value == "idle"
        assert Status.BUSY.value == "busy"
        assert Status.DEGRADED.value == "degraded"
        assert Status.WARNING.value == "warning"
        assert Status.ERROR.value == "error"
        assert Status.SHUTDOWN.value == "shutdown"
        assert Status.FAILED.value == "failed"

    def test_is_healthy(self):
        """Test is_healthy property."""
        assert Status.OK.is_healthy
        assert Status.IDLE.is_healthy
        assert Status.BUSY.is_healthy
        assert Status.DEGRADED.is_healthy
        assert Status.WARNING.is_healthy
        assert not Status.ERROR.is_healthy
        assert not Status.FAILED.is_healthy
        assert not Status.UNKNOWN.is_healthy

    def test_is_operational(self):
        """Test is_operational property."""
        assert Status.STARTUP.is_operational
        assert Status.OK.is_operational
        assert Status.IDLE.is_operational
        assert Status.BUSY.is_operational
        assert Status.DEGRADED.is_operational
        assert Status.WARNING.is_operational
        assert not Status.ERROR.is_operational
        assert not Status.FAILED.is_operational


class TestAggregateStatus:
    """Tests for aggregate_status function."""

    def test_aggregate_empty(self):
        """Test aggregation with no reports."""
        assert aggregate_status([]) == Status.UNKNOWN

    def test_aggregate_single_ok(self):
        """Test aggregation with single OK report."""
        reports = [StatusReport("test", Status.OK)]
        assert aggregate_status(reports) == Status.OK

    def test_aggregate_failed_wins(self):
        """Test that FAILED status takes priority."""
        reports = [
            StatusReport("a", Status.OK),
            StatusReport("b", Status.FAILED),
            StatusReport("c", Status.WARNING),
        ]
        assert aggregate_status(reports) == Status.FAILED

    def test_aggregate_error_wins(self):
        """Test that ERROR takes priority over warnings."""
        reports = [
            StatusReport("a", Status.OK),
            StatusReport("b", Status.ERROR),
            StatusReport("c", Status.WARNING),
        ]
        assert aggregate_status(reports) == Status.ERROR

    def test_aggregate_busy_from_multiple(self):
        """Test that BUSY bubbles up."""
        reports = [
            StatusReport("a", Status.OK),
            StatusReport("b", Status.BUSY),
            StatusReport("c", Status.IDLE),
        ]
        assert aggregate_status(reports) == Status.BUSY

    def test_aggregate_idle_preferred(self):
        """Test that IDLE is preferred over OK when all healthy."""
        reports = [
            StatusReport("a", Status.OK),
            StatusReport("b", Status.IDLE),
        ]
        assert aggregate_status(reports) == Status.IDLE


class TestMonitoredObject:
    """Tests for MonitoredObject base class."""

    def test_create_monitored_object(self):
        """Test creating a MonitoredObject."""
        monitor = MonitoredObject("test")
        assert monitor.name == "test"
        assert monitor.get_status() == Status.UNKNOWN

    def test_set_status(self):
        """Test setting status."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK, "All good")
        assert monitor.get_status() == Status.OK
        assert monitor._message == "All good"

    def test_add_healthcheck_callback(self):
        """Test adding healthcheck callback."""
        monitor = MonitoredObject("test")

        def healthcheck():
            return Status.OK

        monitor.add_healthcheck_cb(healthcheck)
        assert len(monitor._healthcheck_callbacks) == 1

    def test_add_metric_callback(self):
        """Test adding metric callback."""
        monitor = MonitoredObject("test")

        def get_metrics():
            return {"test": 123}

        monitor.add_metric_cb(get_metrics)
        assert len(monitor._metric_callbacks) == 1

    @pytest.mark.asyncio
    async def test_healthcheck_sync_callback(self):
        """Test healthcheck with sync callback returning unhealthy status."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        def healthcheck():
            return Status.ERROR  # Unhealthy status

        monitor.add_healthcheck_cb(healthcheck)
        result = await monitor.healthcheck()
        assert result == Status.ERROR

    @pytest.mark.asyncio
    async def test_healthcheck_async_callback(self):
        """Test healthcheck with async callback returning unhealthy status."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        async def healthcheck():
            return Status.FAILED  # Unhealthy status

        monitor.add_healthcheck_cb(healthcheck)
        result = await monitor.healthcheck()
        assert result == Status.FAILED

    @pytest.mark.asyncio
    async def test_healthcheck_returns_none_when_healthy(self):
        """Test healthcheck returns current status when callbacks return None."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        def healthcheck():
            return None  # Healthy

        monitor.add_healthcheck_cb(healthcheck)
        result = await monitor.healthcheck()
        assert result == Status.OK

    @pytest.mark.asyncio
    async def test_get_full_report_with_metrics(self):
        """Test getting full report with metrics."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        def get_metrics():
            return {"queue_size": 10, "errors": 0}

        monitor.add_metric_cb(get_metrics)
        report = await monitor.get_full_report()

        assert report.name == "test"
        assert report.status == Status.OK
        assert report.details is not None
        assert "metrics" in report.details
        assert report.details["metrics"]["queue_size"] == 10

    @pytest.mark.asyncio
    async def test_send_registration_is_noop(self):
        """Test that send_registration is no-op on base class."""
        monitor = MonitoredObject("test")
        # Should not raise
        await monitor.send_registration()

    @pytest.mark.asyncio
    async def test_send_shutdown_is_noop(self):
        """Test that send_shutdown is no-op on base class."""
        monitor = MonitoredObject("test")
        # Should not raise
        await monitor.send_shutdown()


class TestTaskTracking:
    """Tests for task tracking (BUSY/IDLE)."""

    @pytest.mark.asyncio
    async def test_task_tracking_starts_busy(self):
        """Test that task tracking immediately sets BUSY status."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        async with monitor.track_task():
            # Give time for status change
            await asyncio.sleep(0.01)
            assert monitor.get_status() == Status.BUSY

    @pytest.mark.asyncio
    async def test_task_tracking_transitions_to_idle(self):
        """Test that task tracking transitions to IDLE after delay."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        async with monitor.track_task():
            await asyncio.sleep(0.01)
            assert monitor.get_status() == Status.BUSY

        # Wait for 1s delay before IDLE transition
        await asyncio.sleep(1.1)
        assert monitor.get_status() == Status.IDLE

    @pytest.mark.asyncio
    async def test_multiple_tasks_stay_busy(self):
        """Test that multiple concurrent tasks keep status BUSY."""
        monitor = MonitoredObject("test")
        monitor.set_status(Status.OK)

        async def task():
            async with monitor.track_task():
                await asyncio.sleep(0.1)

        # Start multiple tasks
        tasks = [asyncio.create_task(task()) for _ in range(3)]
        await asyncio.sleep(0.05)

        # Should be BUSY while tasks running
        assert monitor.get_status() == Status.BUSY

        # Wait for all tasks to complete
        await asyncio.gather(*tasks)

        # Wait for IDLE transition
        await asyncio.sleep(1.1)
        assert monitor.get_status() == Status.IDLE

    @pytest.mark.asyncio
    async def test_task_tracking_enabled_flag(self):
        """Test that task tracking enabled flag is set."""
        monitor = MonitoredObject("test")
        assert not monitor._task_tracking_enabled

        async with monitor.track_task():
            await asyncio.sleep(0.01)

        assert monitor._task_tracking_enabled


class TestDummyMonitoredObject:
    """Tests for DummyMonitoredObject."""

    @pytest.mark.asyncio
    async def test_dummy_start_monitoring(self):
        """Test that start_monitoring is no-op."""
        monitor = DummyMonitoredObject("test")
        await monitor.start_monitoring()  # Should not raise

    @pytest.mark.asyncio
    async def test_dummy_stop_monitoring(self):
        """Test that stop_monitoring is no-op."""
        monitor = DummyMonitoredObject("test")
        await monitor.stop_monitoring()  # Should not raise

    @pytest.mark.asyncio
    async def test_dummy_context_manager(self):
        """Test context manager support."""
        monitor = DummyMonitoredObject("test")
        async with monitor:
            monitor.set_status(Status.OK)
        assert monitor.get_status() == Status.OK


class TestReportingMonitoredObject:
    """Tests for ReportingMonitoredObject."""

    @pytest.mark.asyncio
    async def test_start_monitoring_starts_loops(self):
        """Test that start_monitoring starts both loops."""
        monitor = ReportingMonitoredObject("test", check_interval=0.1, healthcheck_interval=0.2)

        await monitor.start_monitoring()
        await asyncio.sleep(0.05)

        assert monitor._running
        assert monitor._heartbeat_task is not None
        assert monitor._healthcheck_task is not None

        await monitor.stop_monitoring()

    @pytest.mark.asyncio
    async def test_stop_monitoring_stops_loops(self):
        """Test that stop_monitoring stops both loops."""
        monitor = ReportingMonitoredObject("test", check_interval=0.1, healthcheck_interval=0.2)

        await monitor.start_monitoring()
        await asyncio.sleep(0.05)
        await monitor.stop_monitoring()

        assert not monitor._running
        assert monitor._heartbeat_task is None
        assert monitor._healthcheck_task is None

    @pytest.mark.asyncio
    async def test_healthcheck_loop_updates_status(self):
        """Test that healthcheck loop auto-updates status."""
        monitor = ReportingMonitoredObject("test", check_interval=0.1, healthcheck_interval=0.2)
        monitor.set_status(Status.OK)

        # Add healthcheck that returns ERROR (unhealthy)
        def healthcheck():
            return Status.ERROR

        monitor.add_healthcheck_cb(healthcheck)

        await monitor.start_monitoring()

        # Wait for healthcheck loop to run
        await asyncio.sleep(0.3)

        assert monitor.get_status() == Status.ERROR

        await monitor.stop_monitoring()


class TestCreateMonitor:
    """Tests for create_monitor factory function."""

    @pytest.mark.asyncio
    async def test_create_monitor_returns_dummy_when_no_messenger(self):
        """Test that create_monitor returns DummyMonitoredObject when no messenger."""
        with patch('ocabox_tcs.management.process_context.ProcessContext.initialize', new_callable=AsyncMock):
            with patch('ocabox_tcs.management.process_context.ProcessContext') as mock_pc:
                mock_instance = Mock()
                mock_instance.messenger = None
                mock_pc.return_value = mock_instance

                monitor = await create_monitor("test")
                assert isinstance(monitor, DummyMonitoredObject)

    @pytest.mark.asyncio
    async def test_create_monitor_returns_dummy_on_exception(self):
        """Test that create_monitor returns DummyMonitoredObject on exception."""
        with patch('ocabox_tcs.management.process_context.ProcessContext.initialize', new_callable=AsyncMock, side_effect=Exception("ProcessContext error")):
            monitor = await create_monitor("test")
            assert isinstance(monitor, DummyMonitoredObject)

    @pytest.mark.asyncio
    async def test_create_monitor_generates_unique_name(self):
        """Test that create_monitor generates unique name when not provided."""
        with patch('ocabox_tcs.management.process_context.ProcessContext.initialize', new_callable=AsyncMock):
            with patch('ocabox_tcs.management.process_context.ProcessContext') as mock_pc:
                mock_instance = Mock()
                mock_instance.messenger = None
                mock_pc.return_value = mock_instance

                monitor = await create_monitor()
                assert monitor.name is not None
                assert len(monitor.name) > 0

    @pytest.mark.asyncio
    async def test_create_monitor_uses_provided_name(self):
        """Test that create_monitor uses provided name."""
        with patch('ocabox_tcs.management.process_context.ProcessContext.initialize', new_callable=AsyncMock):
            with patch('ocabox_tcs.management.process_context.ProcessContext') as mock_pc:
                mock_instance = Mock()
                mock_instance.messenger = None
                mock_pc.return_value = mock_instance

                monitor = await create_monitor(name="my_test")
                assert monitor.name == "my_test"

    @pytest.mark.asyncio
    async def test_create_monitor_with_all_parameters(self):
        """Test that create_monitor accepts all parameters."""
        # Just verify that create_monitor accepts all parameters without error
        with patch('ocabox_tcs.management.process_context.ProcessContext.initialize', new_callable=AsyncMock):
            with patch('ocabox_tcs.management.process_context.ProcessContext') as mock_pc:
                mock_instance = Mock()
                mock_instance.messenger = None
                mock_pc.return_value = mock_instance

                # Should not raise - tests that all params are accepted
                monitor = await create_monitor(
                    name="test",
                    subject_prefix="custom",
                    heartbeat_interval=5.0,
                    healthcheck_interval=15.0,
                    parent_name="parent"
                )
                assert isinstance(monitor, DummyMonitoredObject)


class TestContextManager:
    """Tests for context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_calls_start_stop(self):
        """Test that context manager calls start_monitoring and stop_monitoring."""
        monitor = DummyMonitoredObject("test")

        # Track calls
        start_called = False
        stop_called = False

        original_start = monitor.start_monitoring
        original_stop = monitor.stop_monitoring

        async def tracked_start():
            nonlocal start_called
            start_called = True
            await original_start()

        async def tracked_stop():
            nonlocal stop_called
            stop_called = True
            await original_stop()

        monitor.start_monitoring = tracked_start
        monitor.stop_monitoring = tracked_stop

        async with monitor:
            assert start_called

        assert stop_called


class TestChildMonitors:
    """Tests for parent-child monitor relationships."""

    def test_add_submonitor(self):
        """Test adding a child monitor."""
        parent = MonitoredObject("parent")
        child = MonitoredObject("child", parent=parent)

        assert "child" in parent.children
        assert child.parent == parent

    def test_remove_submonitor(self):
        """Test removing a child monitor."""
        parent = MonitoredObject("parent")
        child = MonitoredObject("child", parent=parent)

        parent.remove_submonitor("child")
        assert "child" not in parent.children
        assert child.parent is None

    @pytest.mark.asyncio
    async def test_get_full_report_includes_children(self):
        """Test that get_full_report includes child reports."""
        parent = MonitoredObject("parent")
        parent.set_status(Status.OK)

        child1 = MonitoredObject("child1", parent=parent)
        child1.set_status(Status.OK)

        child2 = MonitoredObject("child2", parent=parent)
        child2.set_status(Status.WARNING)

        report = await parent.get_full_report()

        assert report.name == "parent"
        # Status should be aggregated from children
        assert report.status == Status.WARNING  # Worst child status
        assert report.details is not None
        assert "children" in report.details
        assert len(report.details["children"]) == 2
