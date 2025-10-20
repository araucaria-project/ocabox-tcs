"""MonitoredObject hierarchy for status reporting and health checking."""

import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from .status import Status, StatusReport, aggregate_status


class MonitoredObject:
    """Base class for monitored objects with aggregation support."""
    
    def __init__(self, name: str, parent: Optional["MonitoredObject"] = None):
        self.name = name
        self.parent = parent
        self.children: dict[str, MonitoredObject] = {}
        self._status = Status.UNKNOWN
        self._message: str | None = None
        self._healthcheck_callbacks: list[Callable[[], Status | None]] = []
        self._status_callbacks: list[Callable[[], Status | None]] = []
        self.logger = logging.getLogger(f"mon.{name}")
        
        if parent:
            parent.add_submonitor(self)
    
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
        """Add healthcheck callback."""
        self._healthcheck_callbacks.append(callback)

    # TODO: Remove status callback. Status have to be set explicite to monitor, monitor should raport it immediately.
    def add_status_cb(self, callback: Callable[[], Status | None]):
        """Add status callback."""
        self._status_callbacks.append(callback)
    
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
        """Get current status with callback evaluation."""
        # Check status callbacks first
        #TODO: No callbacks. Status have to be set explicite to monitor, monitor should raport it immediately.
        for callback in self._status_callbacks:
            try:
                status = callback()
                if status is not None:
                    return status
            except Exception as e:
                self.logger.warning(f"Status callback failed: {e}")
        
        # Fall back to set status
        return self._status
    
    def healthcheck(self) -> Status:
        """Perform health check using callbacks."""
        # Run healthcheck callbacks
        for callback in self._healthcheck_callbacks:
            try:
                status = callback()
                if status is not None and not status.is_healthy:
                    return status
            except Exception as e:
                self.logger.warning(f"Healthcheck callback failed: {e}")
                return Status.ERROR
        
        # If no callbacks or all healthy, return current status
        return self.get_status()
    
    def get_full_report(self) -> StatusReport:
        """Get complete status report including children."""
        # Get own status
        own_status = self.get_status()
        
        # Get children reports
        child_reports = []
        for child in self.children.values():
            child_reports.append(child.get_full_report())
        
        # Aggregate if we have children
        if child_reports:
            child_statuses = [report.status for report in child_reports]
            aggregated = aggregate_status([
                StatusReport(self.name, own_status),
                *[StatusReport(f"child_{i}", status) for i, status in enumerate(child_statuses)]
            ])
        else:
            aggregated = own_status
        
        return StatusReport(
            name=self.name,
            status=aggregated,
            message=self._message,
            details={
                "own_status": own_status.value,
                "children": [report.to_dict() for report in child_reports]
            } if child_reports else None
        )


class ReportingMonitoredObject(MonitoredObject):
    """MonitoredObject that actively sends heartbeats and performs health checks."""

    def __init__(self, name: str, parent: MonitoredObject | None = None,
                 check_interval: float = 10.0):
        super().__init__(name, parent)
        self.check_interval = check_interval  # Heartbeat interval (default 10s)
        self._check_task: asyncio.Task | None = None
        self._running = False
    
    async def start_monitoring(self):
        """Start periodic monitoring."""
        if self._running:
            return
        
        self._running = True
        self._check_task = asyncio.create_task(self._monitoring_loop())
        self.logger.info(f"Started monitoring with {self.check_interval}s interval")
    
    async def stop_monitoring(self):
        """Stop periodic monitoring."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None
        self.logger.info("Stopped monitoring")
    
    async def _monitoring_loop(self):
        """Main monitoring loop - performs healthchecks and sends heartbeats."""
        while self._running:
            try:
                # Perform health check - if status changed, it will auto-send via _on_status_changed()
                status = self.healthcheck()
                if status != self.get_status():
                    self.set_status(status, "Updated from healthcheck")

                # Send heartbeat (periodic alive signal)
                await self._send_heartbeat()

                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(min(self.check_interval, 10.0))

    async def _send_heartbeat(self):
        """Send heartbeat message. Override in subclasses for NATS publishing."""
        self.logger.debug(f"Heartbeat from {self.name}")

    async def _send_status_report(self):
        """Send status report (called when status changes). Override in subclasses."""
        report = self.get_full_report()
        self.logger.debug(f"Status changed to {report.status} - {report.message or 'OK'}")


