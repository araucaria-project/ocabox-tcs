"""MonitoredObject hierarchy for status reporting and health checking."""

import asyncio
import logging
from typing import Optional, Callable, Dict, List, Any
from abc import ABC, abstractmethod
from datetime import datetime

from .status import Status, StatusReport, aggregate_status


class MonitoredObject:
    """Base class for monitored objects with aggregation support."""
    
    def __init__(self, name: str, parent: Optional["MonitoredObject"] = None):
        self.name = name
        self.parent = parent
        self.children: Dict[str, "MonitoredObject"] = {}
        self._status = Status.UNKNOWN
        self._message: Optional[str] = None
        self._healthcheck_callbacks: List[Callable[[], Optional[Status]]] = []
        self._status_callbacks: List[Callable[[], Optional[Status]]] = []
        self.logger = logging.getLogger(f"monitored.{name}")
        
        if parent:
            parent.add_submonitor(self)
    
    def set_status(self, status: Status, message: Optional[str] = None):
        """Set status directly."""
        self._status = status
        self._message = message
        self.logger.debug(f"Status set to {status}: {message or ''}")
    
    def add_healthcheck_cb(self, callback: Callable[[], Optional[Status]]):
        """Add healthcheck callback."""
        self._healthcheck_callbacks.append(callback)
    
    def add_status_cb(self, callback: Callable[[], Optional[Status]]):
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
    """MonitoredObject that actively checks status periodically."""
    
    def __init__(self, name: str, parent: Optional[MonitoredObject] = None, 
                 check_interval: float = 30.0):
        super().__init__(name, parent)
        self.check_interval = check_interval
        self._check_task: Optional[asyncio.Task] = None
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
        """Main monitoring loop."""
        while self._running:
            try:
                # Perform health check
                status = self.healthcheck()
                if status != self.get_status():
                    self.set_status(status, "Updated from healthcheck")
                
                # Send report
                await self._send_report()
                
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(min(self.check_interval, 10.0))
    
    async def _send_report(self):
        """Send status report. Override in subclasses."""
        report = self.get_full_report()
        self.logger.debug(f"Status report: {report.status} - {report.message or 'OK'}")


class MessengerMonitoredObject(ReportingMonitoredObject):
    """MonitoredObject that sends reports to NATS via serverish.Messenger."""
    
    def __init__(self, name: str, messenger, parent: Optional[MonitoredObject] = None,
                 check_interval: float = 30.0, topic_prefix: str = "services"):
        super().__init__(name, parent, check_interval)
        self.messenger = messenger
        self.topic_prefix = topic_prefix
    
    async def _send_report(self):
        """Send status report to NATS."""
        try:
            report = self.get_full_report()
            topic = f"{self.topic_prefix}.{self.name}.status"
            
            await self.messenger.publish(topic, report.to_dict())
            self.logger.debug(f"Sent status report to {topic}")
        except Exception as e:
            self.logger.error(f"Failed to send status report: {e}")
    
    async def send_registration(self):
        """Send registration message for discovery."""
        try:
            topic = f"{self.topic_prefix}.discovery"
            message = {
                "action": "register",
                "name": self.name,
                "timestamp": datetime.utcnow().isoformat(),
                "status": self.get_status().value
            }
            await self.messenger.publish(topic, message)
            self.logger.info(f"Sent registration to {topic}")
        except Exception as e:
            self.logger.error(f"Failed to send registration: {e}")
    
    async def send_shutdown(self):
        """Send shutdown message."""
        try:
            topic = f"{self.topic_prefix}.discovery"
            message = {
                "action": "shutdown",
                "name": self.name,
                "timestamp": datetime.utcnow().isoformat()
            }
            await self.messenger.publish(topic, message)
            self.logger.info(f"Sent shutdown message to {topic}")
        except Exception as e:
            self.logger.error(f"Failed to send shutdown message: {e}")