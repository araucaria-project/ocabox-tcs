"""MonitoredObject hierarchy for status reporting and health checking."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Optional

from ocabox_tcs.monitoring.status import Status, StatusReport, aggregate_status


class MonitoredObject:
    """Base class for monitored objects with aggregation support."""

    def __init__(self, name: str, parent: Optional["MonitoredObject"] = None):
        self.name = name
        self.parent = parent
        self.children: dict[str, MonitoredObject] = {}
        self._status = Status.UNKNOWN
        self._message: str | None = None
        self._healthcheck_callbacks: list[Callable[[], Status | None]] = []
        self._metric_callbacks: list[Callable[[], dict[str, Any]]] = []
        self.logger = logging.getLogger(f"mon.{name}")

        # Task tracking for BUSY/IDLE status
        self._active_tasks = 0  # Number of currently running tasks
        self._idle_transition_task: asyncio.Task | None = None  # Delayed IDLE transition
        self._task_tracking_enabled = False  # Whether to use BUSY/IDLE statuses

        if parent:
            parent.add_submonitor(self)

    # Context manager support
    async def __aenter__(self):
        """Enter context: start monitoring."""
        await self.start_monitoring()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit context: stop monitoring."""
        await self.stop_monitoring()
        return False

    async def start_monitoring(self):
        """Start monitoring. Override in subclasses."""
        pass

    async def stop_monitoring(self):
        """Stop monitoring. Override in subclasses."""
        pass

    async def send_registration(self):
        """Send registration event. Override in subclasses that support it."""
        pass

    async def send_shutdown(self):
        """Send shutdown event. Override in subclasses that support it."""
        pass

    def track_task(self):
        """Context manager for tracking task execution (enables BUSY/IDLE status).

        Usage:
            >>> async with monitor.track_task():
            ...     await do_work()

        Behavior:
            - On first use, enables task tracking and switches from OK to BUSY/IDLE
            - Task start: immediately switches to BUSY
            - Task end: waits 1s before switching to IDLE
            - If another task starts during 1s delay, stays BUSY

        Returns:
            TaskTracker context manager
        """
        return _TaskTracker(self)

    async def _task_started(self):
        """Internal: Called when a task starts."""
        self._active_tasks += 1

        # Enable task tracking on first task
        if not self._task_tracking_enabled:
            self._task_tracking_enabled = True
            self.logger.debug("Task tracking enabled")

        # Cancel any pending IDLE transition
        if self._idle_transition_task and not self._idle_transition_task.done():
            self._idle_transition_task.cancel()
            try:
                await self._idle_transition_task
            except asyncio.CancelledError:
                pass
            self._idle_transition_task = None

        # Switch to BUSY immediately if not already
        if self._status not in (Status.BUSY, Status.ERROR, Status.FAILED):
            self.set_status(Status.BUSY, f"Processing tasks ({self._active_tasks} active)")

    async def _task_finished(self):
        """Internal: Called when a task finishes."""
        self._active_tasks -= 1

        if self._active_tasks < 0:
            self.logger.warning("Task counter went negative, resetting to 0")
            self._active_tasks = 0

        # If no more active tasks, schedule transition to IDLE after 1s
        if self._active_tasks == 0:
            self._idle_transition_task = asyncio.create_task(self._delayed_idle_transition())
        else:
            # Still have active tasks, update message
            self.set_status(Status.BUSY, f"Processing tasks ({self._active_tasks} active)")

    async def _delayed_idle_transition(self):
        """Internal: Wait 1s then transition to IDLE if no new tasks."""
        try:
            await asyncio.sleep(1.0)
            # Double-check no tasks started during the delay
            if self._active_tasks == 0 and self._status == Status.BUSY:
                self.set_status(Status.IDLE, "No active tasks")
        except asyncio.CancelledError:
            # Another task started, stay BUSY
            pass

    def set_status(self, status: Status, message: str | None = None):
        """Set status directly and trigger status change notification."""
        old_status = self._status
        self._status = status
        self._message = message
        self.logger.debug(f"Status set to {status}: {message or ''}")

        # Notify if status changed
        if old_status != status:
            self._on_status_changed()

    def _on_status_changed(self):
        """Called when status changes. Override in subclasses to send status updates."""
        pass

    def add_healthcheck_cb(self, callback: Callable[[], Status | None]):
        """Add healthcheck callback (sync or async).

        Callbacks are called periodically during healthcheck loop (default 30s).
        If any callback returns unhealthy status, monitoring auto-updates to that status.

        Args:
            callback: Sync or async callable returning Status or None
                     Return None to indicate healthy/no opinion
                     Return Status to indicate specific health state

        Example:
            >>> def check_connection():
            ...     if not self.connected:
            ...         return Status.ERROR
            ...     return None  # Healthy
            >>> monitor.add_healthcheck_cb(check_connection)

            >>> async def check_async():
            ...     if await self.ping_failed():
            ...         return Status.DEGRADED
            ...     return None
            >>> monitor.add_healthcheck_cb(check_async)
        """
        self._healthcheck_callbacks.append(callback)

    def add_metric_cb(self, callback: Callable[[], dict[str, Any]]):
        """Add metric callback (sync or async).

        Metric callbacks are called when generating status reports to collect
        custom metrics/statistics. Returned data is included in the report's
        details section.

        Args:
            callback: Sync or async callable returning dict of metrics
                     Keys should be metric names, values can be any JSON-serializable data

        Example:
            >>> def get_metrics():
            ...     return {
            ...         "queue_size": len(self.queue),
            ...         "error_count": self.errors,
            ...         "last_update": time.time()
            ...     }
            >>> monitor.add_metric_cb(get_metrics)

            >>> async def get_async_metrics():
            ...     return {
            ...         "active_connections": await self.count_connections(),
            ...         "avg_latency_ms": await self.get_avg_latency()
            ...     }
            >>> monitor.add_metric_cb(get_async_metrics)
        """
        self._metric_callbacks.append(callback)


    def add_submonitor(self, child: "MonitoredObject"):
        """Add child monitor."""
        self.children[child.name] = child
        child.parent = self
        self.logger.debug(f"Added submonitor: {child.name}")

    def remove_submonitor(self, name: str):
        """Remove child monitor."""
        if name in self.children:
            self.children[name].parent = None
            del self.children[name]
            self.logger.debug(f"Removed submonitor: {name}")

    def get_status(self) -> Status:
        """Get current status """
        return self._status

    async def healthcheck(self) -> Status:
        """Perform health check using callbacks (supports sync and async).

        Returns:
            Status from first unhealthy callback, or current status if all healthy
        """
        # Run healthcheck callbacks (both sync and async)
        for callback in self._healthcheck_callbacks:
            try:
                # Check if callback is async
                if asyncio.iscoroutinefunction(callback):
                    status = await callback()
                else:
                    status = callback()

                if status is not None and not status.is_healthy:
                    return status
            except Exception as e:
                self.logger.warning(f"Healthcheck callback failed: {e}")
                return Status.ERROR

        # If no callbacks or all healthy, return current status
        return self.get_status()

    async def get_full_report(self) -> StatusReport:
        """Get complete status report including children and metrics."""
        # Get own status
        own_status = self.get_status()

        # Collect metrics from callbacks (both sync and async)
        metrics = {}
        for callback in self._metric_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    metric_data = await callback()
                else:
                    metric_data = callback()

                if metric_data and isinstance(metric_data, dict):
                    metrics.update(metric_data)
            except Exception as e:
                self.logger.warning(f"Metric callback failed: {e}")

        # Get children reports
        child_reports = []
        for child in self.children.values():
            child_reports.append(await child.get_full_report())

        # Aggregate if we have children
        if child_reports:
            child_statuses = [report.status for report in child_reports]
            aggregated = aggregate_status([
                StatusReport(self.name, own_status),
                *[StatusReport(f"child_{i}", status) for i, status in enumerate(child_statuses)]
            ])
        else:
            aggregated = own_status

        # Build details section
        details = None
        if child_reports or metrics:
            details = {}
            if child_reports:
                details["own_status"] = own_status.value
                details["children"] = [report.to_dict() for report in child_reports]
            if metrics:
                details["metrics"] = metrics

        return StatusReport(
            name=self.name,
            status=aggregated,
            message=self._message,
            details=details
        )


class _TaskTracker:
    """Context manager for tracking task execution in MonitoredObject."""

    def __init__(self, monitor: MonitoredObject):
        self.monitor = monitor

    async def __aenter__(self):
        """Task starting."""
        await self.monitor._task_started()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Task finished."""
        await self.monitor._task_finished()
        return False


class ReportingMonitoredObject(MonitoredObject):
    """MonitoredObject that actively sends heartbeats and performs health checks."""

    def __init__(
        self,
        name: str,
        parent: MonitoredObject | None = None,
        check_interval: float = 10.0,
        healthcheck_interval: float = 30.0,
    ):
        super().__init__(name, parent)
        self.check_interval = check_interval  # Heartbeat interval (default 10s)
        self.healthcheck_interval = healthcheck_interval  # Healthcheck interval (default 30s)
        self._heartbeat_task: asyncio.Task | None = None
        self._healthcheck_task: asyncio.Task | None = None
        self._running = False

    async def start_monitoring(self):
        """Start periodic monitoring (heartbeat and healthcheck loops)."""
        if self._running:
            return

        self._running = True

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Start healthcheck loop
        self._healthcheck_task = asyncio.create_task(self._healthcheck_loop())

        self.logger.info(
            f"Started monitoring: heartbeat={self.check_interval}s, "
            f"healthcheck={self.healthcheck_interval}s"
        )

    async def stop_monitoring(self):
        """Stop periodic monitoring (both loops)."""
        self._running = False

        # Stop heartbeat loop
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Stop healthcheck loop
        if self._healthcheck_task:
            self._healthcheck_task.cancel()
            try:
                await self._healthcheck_task
            except asyncio.CancelledError:
                pass
            self._healthcheck_task = None

        self.logger.info("Stopped monitoring")

    async def _heartbeat_loop(self):
        """Heartbeat loop - sends periodic heartbeats."""
        while self._running:
            try:
                await self._send_heartbeat()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Heartbeat loop error: {e}")
                await asyncio.sleep(min(self.check_interval, 10.0))

    async def _healthcheck_loop(self):
        """Healthcheck loop - performs periodic health checks and updates status."""
        while self._running:
            try:
                # Perform health check (supports sync and async callbacks)
                status = await self.healthcheck()
                # Auto-update status if changed (triggers _on_status_changed)
                if status != self.get_status():
                    self.set_status(status, "Updated from healthcheck")

                await asyncio.sleep(self.healthcheck_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Healthcheck loop error: {e}")
                await asyncio.sleep(min(self.healthcheck_interval, 10.0))

    async def _send_heartbeat(self):
        """Send heartbeat message. Override in subclasses for NATS publishing."""
        self.logger.debug(f"Heartbeat from {self.name}")

    async def _send_status_report(self):
        """Send status report (called when status changes). Override in subclasses."""
        report = await self.get_full_report()
        self.logger.debug(f"Status changed to {report.status} - {report.message or 'OK'}")


class DummyMonitoredObject(MonitoredObject):
    """No-op monitored object (when NATS not available).

    Provides the same API as other MonitoredObject implementations but does nothing.
    Useful for:
    - Development without NATS
    - Testing
    - Environments where monitoring is disabled
    """

    async def start_monitoring(self):
        """No-op start monitoring."""
        self.logger.debug(f"DummyMonitor: start_monitoring() called for {self.name}")

    async def stop_monitoring(self):
        """No-op stop monitoring."""
        self.logger.debug(f"DummyMonitor: stop_monitoring() called for {self.name}")

    def _on_status_changed(self):
        """No-op status change notification."""
        self.logger.debug(f"DummyMonitor: status changed to {self._status}")

