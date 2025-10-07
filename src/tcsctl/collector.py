"""Data collector for TCS services from NATS streams."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from serverish.base import dt_from_array
from serverish.base.exceptions import MessengerReaderStopped
from serverish.messenger import Messenger
from serverish.messenger.msg_reader import MsgReader

from ocabox_tcs.monitoring import Status

logger = logging.getLogger(__name__)


@dataclass
class ServiceInfo:
    """Information about a TCS service."""
    service_id: str
    status: Status
    status_message: Optional[str] = None
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    uptime_seconds: Optional[float] = None

    # Detailed metadata fields
    runner_id: Optional[str] = None
    hostname: Optional[str] = None
    last_status_update: Optional[datetime] = None
    pid: Optional[int] = None

    @property
    def is_running(self) -> bool:
        """Check if service is currently running."""
        return self.status.is_operational and self.stop_time is None

    @property
    def uptime_str(self) -> str:
        """Human-readable uptime."""
        if self.uptime_seconds is None:
            return "N/A"

        if self.uptime_seconds < 60:
            return f"{int(self.uptime_seconds)}s"
        elif self.uptime_seconds < 3600:
            return f"{int(self.uptime_seconds / 60)}m"
        elif self.uptime_seconds < 86400:
            hours = int(self.uptime_seconds / 3600)
            mins = int((self.uptime_seconds % 3600) / 60)
            return f"{hours}h {mins}m"
        else:
            days = int(self.uptime_seconds / 86400)
            hours = int((self.uptime_seconds % 86400) / 3600)
            return f"{days}d {hours}h"

    @property
    def heartbeat_status(self) -> str:
        """Status of heartbeat (alive, stale, dead, none).

        For running services, missing heartbeat is treated as 'dead' (zombie process).
        Only stopped services should return 'none'.
        """
        if self.last_heartbeat is None:
            # Running service without heartbeat = zombie (dead)
            # Stopped service without heartbeat = expected (none)
            return "dead" if self.is_running else "none"

        age = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        if age < 30:  # Within 3x heartbeat interval
            return "alive"
        elif age < 120:  # Within ~2 minutes
            return "stale"
        else:
            return "dead"


async def collect_services_info(host: str = 'localhost', port: int = 4222,
                                subject_prefix: str = 'svc') -> list[ServiceInfo]:
    """Collect information about all TCS services from NATS streams.

    Args:
        host: NATS server host (default: 'localhost')
        port: NATS server port (default: 4222)
        subject_prefix: Prefix for service subjects (default: 'svc.')

    Returns:
        List of ServiceInfo objects for all discovered services
    """
    services = {}  # service_id -> ServiceInfo

    messenger = Messenger()
    async with messenger.context(host=host, port=port):
        # Read all three streams in parallel with nowait=True
        # This finishes immediately when all existing messages are consumed

        async def read_registry():
            """Read all service lifecycle events."""
            start_time = time.time()
            msg_count = 0

            registry_reader = MsgReader(
                subject=f"{subject_prefix}.registry.>",
                parent=messenger,
                deliver_policy="all",
                nowait=True,
            )
            async with registry_reader:
                async for data, meta in registry_reader:
                    msg_count += 1
                    event = data.get('event')
                    service_id = data.get('service_id')

                    if not service_id:
                        continue

                    # Initialize service info if new
                    if service_id not in services:
                        services[service_id] = ServiceInfo(
                            service_id=service_id,
                            status=Status.UNKNOWN
                        )

                    # Update based on event type
                    if event == 'start':
                        timestamp = data.get('timestamp')
                        if timestamp:
                            services[service_id].start_time = dt_from_array(timestamp)
                            services[service_id].stop_time = None  # Clear stop if restarted

                        # Extract metadata from start event
                        if 'runner_id' in data:
                            services[service_id].runner_id = data['runner_id']
                        if 'hostname' in data:
                            services[service_id].hostname = data['hostname']
                        if 'pid' in data:
                            services[service_id].pid = data['pid']

                    elif event == 'stop':
                        timestamp = data.get('timestamp')
                        if timestamp:
                            services[service_id].stop_time = dt_from_array(timestamp)

            elapsed = time.time() - start_time
            logger.info(f"read_registry: {msg_count} messages in {elapsed:.3f}s")

        async def read_status():
            """Read status updates from last 24h."""
            start_time = time.time()
            msg_count = 0

            since_time = datetime.now(timezone.utc) - timedelta(hours=24)
            status_reader = MsgReader(
                subject=f'{subject_prefix}.status.>',
                parent=messenger,
                deliver_policy='by_start_time',
                opt_start_time=since_time,
                nowait=True
            )
            async with status_reader:
                async for data, meta in status_reader:
                    msg_count += 1
                    service_id = data.get('name')
                    if not service_id or service_id not in services:
                        continue

                    # Use latest status
                    status_str = data.get('status')
                    if status_str:
                        services[service_id].status = Status(status_str)
                        services[service_id].status_message = data.get('message')

                        # Capture last status update timestamp from meta
                        if 'timestamp' in meta:
                            ts = meta['timestamp']
                            if isinstance(ts, list):
                                services[service_id].last_status_update = dt_from_array(ts)

            elapsed = time.time() - start_time
            logger.info(f"read_status: {msg_count} messages in {elapsed:.3f}s")

        async def read_heartbeats():
            """Read heartbeats from last 10 minutes."""
            start_time = time.time()
            msg_count = 0

            since_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            heartbeat_reader = MsgReader(
                subject=f"{subject_prefix}.heartbeat.>",
                parent=messenger,
                deliver_policy="by_start_time",
                opt_start_time=since_time,
                nowait=True,
            )
            async with heartbeat_reader:
                async for data, meta in heartbeat_reader:
                    msg_count += 1
                    service_id = data.get('service_id')
                    if not service_id or service_id not in services:
                        continue

                    # Use latest heartbeat
                    timestamp = data.get('timestamp')
                    if timestamp:
                        hb_time = dt_from_array(timestamp)
                        if (services[service_id].last_heartbeat is None or
                            hb_time > services[service_id].last_heartbeat):
                            services[service_id].last_heartbeat = hb_time

            elapsed = time.time() - start_time
            logger.info(f"read_heartbeats: {msg_count} messages in {elapsed:.3f}s")

        # Read registry first to populate service entries, then read status/heartbeats in parallel
        # This ensures services dict is populated before status and heartbeats try to update it
        await read_registry()
        await asyncio.gather(
            read_status(),
            read_heartbeats()
        )

    # Calculate uptime for running services
    now = datetime.now(timezone.utc)
    for service in services.values():
        if service.start_time and not service.stop_time:
            service.uptime_seconds = (now - service.start_time).total_seconds()

    return list(services.values())
