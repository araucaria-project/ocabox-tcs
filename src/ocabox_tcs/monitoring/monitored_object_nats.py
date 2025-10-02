from serverish.base import dt_utcnow_array
from serverish.messenger import get_publisher, single_publish

from ocabox_tcs.monitoring import MonitoredObject, ReportingMonitoredObject


class MessengerMonitoredObject(ReportingMonitoredObject):
    """MonitoredObject that sends reports to NATS via serverish.Messenger.

    Conforms to NATS subject schema from nats.md:
    - Status updates: <prefix>.status.<service_name>
    - Registry events: <prefix>.registry.<event>.<service_name>

    Args:
        name: Service name (e.g., "guider.jk15")
        messenger: Serverish Messenger instance
        parent: Parent MonitoredObject (optional)
        check_interval: Health check interval in seconds (default: 30.0)
        subject_prefix: NATS subject prefix (default: "svc")
            Can be configured for different installations (e.g., "ocm.svc")
    """

    def __init__(self, name: str, messenger, parent: MonitoredObject | None = None,
                 check_interval: float = 10.0, subject_prefix: str = "svc"):
        super().__init__(name, parent, check_interval)
        self.messenger = messenger
        self.subject_prefix = subject_prefix

        # Publisher for repeated status messages
        self._status_publisher = None
        if messenger is not None:
            status_subject = f"{self.subject_prefix}.status.{self.name}"
            self._status_publisher = get_publisher(status_subject)

    async def _send_report(self):
        """Send status report to NATS.

        Subject: <prefix>.status.<service_name>
        Uses MsgPublisher for repeated status updates.
        """
        if self._status_publisher is None:
            self.logger.debug("Status publisher not set, cannot send report")
            return
        try:
            report = self.get_full_report()
            await self._status_publisher.publish(data=report.to_dict())
            self.logger.debug(f"Sent STATUS report to {self._status_publisher.subject}")
        except Exception as e:
            self.logger.error(f"Failed to send STATUS report: {e}")

    async def send_registration(self):
        """Send start event to NATS registry.

        Subject: <prefix>.registry.start.<service_name>
        Uses single_publish for one-time lifecycle event.
        """
        if self.messenger is None:
            self.logger.warning("Messenger not set, cannot send registration")
            return
        try:
            subject = f"{self.subject_prefix}.registry.start.{self.name}"
            data = {
                "event": "start",
                "service_id": self.name,
                "timestamp": dt_utcnow_array(),
                "status": self.get_status().value
            }
            await single_publish(subject, data)
            self.logger.info(f"Sent START message to {subject}")
        except Exception as e:
            self.logger.error(f"Failed to send START message: {e}")

    async def send_shutdown(self):
        """Send stop event to NATS registry.

        Subject: <prefix>.registry.stop.<service_name>
        Uses single_publish for one-time lifecycle event.
        """
        if self.messenger is None:
            self.logger.warning("Messenger not set, cannot send shutdown")
            return
        try:
            subject = f"{self.subject_prefix}.registry.stop.{self.name}"
            data = {
                "event": "stop",
                "service_id": self.name,
                "timestamp": dt_utcnow_array()
            }
            await single_publish(subject, data)
            self.logger.info(f"Sent STOP message to {subject}")
        except Exception as e:
            self.logger.error(f"Failed to send STOP message: {e}")
