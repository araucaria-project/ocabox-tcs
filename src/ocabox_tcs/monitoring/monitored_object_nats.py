from serverish.base import dt_utcnow_array
from serverish.messenger import get_publisher

from ocabox_tcs.monitoring.monitored_object import MonitoredObject, ReportingMonitoredObject


class MessengerMonitoredObject(ReportingMonitoredObject):
    """MonitoredObject that sends reports to NATS via serverish.Messenger.

    Conforms to NATS subject schema from nats.md:
    - Status updates: <prefix>.status.<service_name> (on status change)
    - Heartbeat: <prefix>.heartbeat.<service_name> (periodic, default 10s)
    - Registry events: <prefix>.registry.<event>.<service_name>

    Args:
        name: Service name (e.g., "guider.jk15")
        messenger: Serverish Messenger instance
        parent: Parent MonitoredObject (optional, for internal hierarchy)
        check_interval: Heartbeat interval in seconds (default: 10.0)
        subject_prefix: NATS subject prefix (default: "svc")
            Can be configured for different installations (e.g., "ocm.svc")
        parent_name: Optional parent name for grouping in displays (default: None)
            Used by monitoring tools to group entities hierarchically
    """

    def __init__(
        self,
        name: str,
        messenger,
        parent: MonitoredObject | None = None,
        check_interval: float = 10.0,
        healthcheck_interval: float = 30.0,
        subject_prefix: str = "svc",
        parent_name: str | None = None,
    ):
        super().__init__(name, parent, check_interval, healthcheck_interval)
        self.messenger = messenger
        self.subject_prefix = subject_prefix
        self.parent_name = parent_name  # For display grouping (e.g., launcher name)

        # Cache PID and hostname (expensive operations, don't change during runtime)
        import os
        import socket

        self._cached_pid = os.getpid()
        self._cached_hostname = socket.gethostname()

        # Publisher for status changes (sent immediately on status change)
        self._status_publisher = None
        # Publisher for periodic heartbeats (sent every check_interval)
        self._heartbeat_publisher = None

        if messenger is not None:
            status_subject = f"{self.subject_prefix}.status.{self.name}"
            self._status_publisher = get_publisher(status_subject)

            heartbeat_subject = f"{self.subject_prefix}.heartbeat.{self.name}"
            self._heartbeat_publisher = get_publisher(heartbeat_subject)

    # Context manager override for registry events
    async def __aenter__(self):
        """Enter context: start monitoring and send registration."""
        await self.start_monitoring()
        await self.send_registration()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit context: send shutdown and stop monitoring."""
        await self.send_shutdown()
        await self.stop_monitoring()
        return False

    def _on_status_changed(self):
        """Called when status changes - trigger immediate status send."""
        # Use asyncio to schedule the async send (called from sync context)
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._send_status_report())
        except RuntimeError:
            # No event loop running - skip status send
            pass

    async def _send_status_report(self):
        """Send status report to NATS (called when status changes).

        Subject: <prefix>.status.<service_name>
        Uses MsgPublisher for status updates.
        """
        if self._status_publisher is None:
            self.logger.debug("Status publisher not set, cannot send status report")
            return
        try:
            report = await self.get_full_report()
            data = report.to_dict()
            # Add parent_name if set (for display grouping)
            if self.parent_name:
                data["parent"] = self.parent_name
            # Add cached PID and hostname for monitoring
            data["pid"] = self._cached_pid
            data["hostname"] = self._cached_hostname
            await self._status_publisher.publish(data=data)
            self.logger.debug(f"Sent STATUS report to {self._status_publisher.subject}")
        except Exception as e:
            self.logger.error(f"Failed to send STATUS report: {e}")

    async def _send_heartbeat(self):
        """Send heartbeat to NATS (periodic alive signal).

        Subject: <prefix>.heartbeat.<service_name>
        Uses MsgPublisher for periodic heartbeat messages.
        """
        if self._heartbeat_publisher is None:
            self.logger.debug("Heartbeat publisher not set, cannot send heartbeat")
            return
        try:
            data = {
                "service_id": self.name,
                "timestamp": dt_utcnow_array(),
                "status": self.get_status().value,  # Include current status in heartbeat
            }
            await self._heartbeat_publisher.publish(data=data)
            self.logger.debug(f"Sent HEARTBEAT to {self._heartbeat_publisher.subject}")
        except Exception as e:
            self.logger.error(f"Failed to send HEARTBEAT: {e}")

    async def send_registration(self):
        """No-op: Registry/lifecycle events are published by runners.

        MonitoredObject only publishes:
        - Status updates (on status change)
        - Heartbeats (periodic)

        Lifecycle/registry events (start, stop, crashed, etc.) are the
        responsibility of runners (ProcessRunner, StandaloneRunner, etc.).
        This keeps MonitoredObject reusable for any monitored entity.
        """
        self.logger.debug("send_registration() called (no-op - runners handle registry events)")

    async def send_shutdown(self):
        """No-op: Registry/lifecycle events are published by runners.

        See send_registration() for explanation.
        """
        self.logger.debug("send_shutdown() called (no-op - runners handle registry events)")
