from datetime import datetime
from typing import Optional

from ocabox_tcs.monitoring import ReportingMonitoredObject, MonitoredObject


class MessengerMonitoredObject(ReportingMonitoredObject):
    """MonitoredObject that sends reports to NATS via serverish.Messenger."""

    def __init__(self, name: str, messenger, parent: Optional[MonitoredObject] = None,
                 check_interval: float = 30.0, topic_prefix: str = "services"):
        super().__init__(name, parent, check_interval)
        self.messenger = messenger
        self.topic_prefix = topic_prefix

    async def _send_report(self):
        """Send status report to NATS."""
        if self.messenger is None:
            self.logger.debug("Messenger not set, cannot send report")
            return
        try:
            report = self.get_full_report()
            topic = f"{self.topic_prefix}.{self.name}.status"

            await self.messenger.publish(topic, report.to_dict())
            self.logger.debug(f"Sent status report to {topic}")
        except Exception as e:
            self.logger.error(f"Failed to send status report: {e}")

    async def send_registration(self):
        """Send registration message for discovery."""
        if self.messenger is None:
            self.logger.warning("Messenger not set, cannot send registration")
            return
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
        if self.messenger is None:
            self.logger.warning("Messenger not set, cannot send shutdown")
            return
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
