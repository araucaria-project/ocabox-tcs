"""ServiceControlClient for programmatic access to TCS service monitoring.

This module provides a reusable client for monitoring TCS services, suitable for
integration into other monitoring tools and applications.

Example usage:

    One-shot mode (snapshot):
        from tcsctl import ServiceControlClient
        from serverish.messenger import Messenger

        messenger = Messenger()
        async with messenger.context(host='localhost', port=4222):
            client = ServiceControlClient(messenger, subject_prefix='svc')
            services = await client.list_services(include_stopped=False)
            service = await client.get_service('hello_world:main')

    Streaming mode (follow):
        messenger = Messenger()
        async with messenger.context(host='localhost', port=4222):
            client = ServiceControlClient(messenger, subject_prefix='svc')

            def on_update(service_info):
                print(f"Updated: {service_info.service_id} -> {service_info.status}")

            client.on_service_update = on_update
            await client.start_following()

            # Access current state anytime
            services = client.get_current_services()

            await asyncio.sleep(60)
            await client.stop_following()
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from serverish.base import dt_from_array
from serverish.messenger import Messenger
from serverish.messenger.msg_reader import MsgReader

from ocabox_tcs.monitoring import Status
from tcsctl.collector import ServiceInfo

logger = logging.getLogger(__name__)


class ServiceControlClient:
    """Client for monitoring TCS services via NATS.

    Supports both one-shot (snapshot) and streaming (follow) modes.
    Assumes an already-open Messenger instance.
    """

    def __init__(self, messenger: Messenger, subject_prefix: str = 'svc'):
        """Initialize the service control client.

        Args:
            messenger: Already-open Messenger instance for NATS communication
            subject_prefix: NATS subject prefix for service messages (default: 'svc')
        """
        self.messenger = messenger
        self.subject_prefix = subject_prefix

        # Internal state (for streaming mode)
        self._services: dict[str, ServiceInfo] = {}
        self._following = False
        self._follow_tasks: list[asyncio.Task] = []

        # Callbacks for streaming mode
        self.on_service_update: Optional[Callable[[ServiceInfo], None]] = None
        self.on_service_start: Optional[Callable[[ServiceInfo], None]] = None
        self.on_service_stop: Optional[Callable[[ServiceInfo], None]] = None

        logger.debug(f"ServiceControlClient initialized with prefix '{subject_prefix}'")

    # ========== One-shot Methods (Snapshot) ==========

    async def list_services(self, include_stopped: bool = False) -> list[ServiceInfo]:
        """Collect current snapshot of all services.

        Args:
            include_stopped: Include stopped services in results

        Returns:
            List of ServiceInfo objects
        """
        services = await self._collect_snapshot()

        if not include_stopped:
            services = [s for s in services if s.is_running]

        return services

    async def get_service(self, service_id: str) -> Optional[ServiceInfo]:
        """Get information about a specific service.

        Args:
            service_id: Service identifier (e.g., 'hello_world:main')

        Returns:
            ServiceInfo if found, None otherwise
        """
        services = await self._collect_snapshot()

        for service in services:
            if service.service_id == service_id:
                return service

        return None

    # ========== Streaming Methods (Follow Mode) ==========

    async def start_following(self):
        """Start following service updates in real-time.

        Subscribes to NATS streams and continuously updates internal state.
        Use get_current_services() to access current state.
        Callbacks (on_service_update, on_service_start, on_service_stop) will be
        invoked when state changes.
        """
        if self._following:
            logger.warning("Already following services")
            return

        logger.info("Starting service following mode")

        # First, collect initial snapshot
        self._services = {s.service_id: s for s in await self._collect_snapshot()}

        # Start streaming tasks
        self._following = True
        self._follow_tasks = [
            asyncio.create_task(self._follow_registry()),
            asyncio.create_task(self._follow_status()),
            asyncio.create_task(self._follow_heartbeats()),
        ]

        logger.info(f"Following {len(self._services)} services")

    async def stop_following(self):
        """Stop following service updates."""
        if not self._following:
            return

        logger.info("Stopping service following mode")
        self._following = False

        # Cancel all follow tasks
        for task in self._follow_tasks:
            task.cancel()

        # Wait for tasks to complete
        await asyncio.gather(*self._follow_tasks, return_exceptions=True)

        self._follow_tasks = []
        logger.info("Service following stopped")

    def get_current_services(self, include_stopped: bool = False) -> list[ServiceInfo]:
        """Get current services from internal state (streaming mode only).

        Only valid after start_following() has been called.

        Args:
            include_stopped: Include stopped services in results

        Returns:
            List of ServiceInfo objects from current state
        """
        if not self._following and not self._services:
            logger.warning("Not following and no state available. Call start_following() first.")
            return []

        services = list(self._services.values())

        if not include_stopped:
            services = [s for s in services if s.is_running]

        return services

    def get_current_service(self, service_id: str) -> Optional[ServiceInfo]:
        """Get specific service from internal state (streaming mode only).

        Args:
            service_id: Service identifier

        Returns:
            ServiceInfo if found, None otherwise
        """
        return self._services.get(service_id)

    # ========== Internal Methods ==========

    async def _collect_snapshot(self) -> list[ServiceInfo]:
        """Collect one-time snapshot of all services.

        Returns:
            List of ServiceInfo objects
        """
        services: dict[str, ServiceInfo] = {}

        # Read all three streams in parallel with nowait=True
        # This finishes immediately when all existing messages are consumed

        async def read_registry():
            """Read all service lifecycle events."""
            start_time = time.time()
            msg_count = 0

            registry_reader = MsgReader(
                subject=f"{self.subject_prefix}.registry.>",
                parent=self.messenger,
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
            logger.debug(f"read_registry: {msg_count} messages in {elapsed:.3f}s")

        async def read_status():
            """Read status updates from last 24h."""
            start_time = time.time()
            msg_count = 0

            since_time = datetime.now(timezone.utc) - timedelta(hours=24)
            status_reader = MsgReader(
                subject=f'{self.subject_prefix}.status.>',
                parent=self.messenger,
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
            logger.debug(f"read_status: {msg_count} messages in {elapsed:.3f}s")

        async def read_heartbeats():
            """Read heartbeats from last 10 minutes."""
            start_time = time.time()
            msg_count = 0

            since_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            heartbeat_reader = MsgReader(
                subject=f"{self.subject_prefix}.heartbeat.>",
                parent=self.messenger,
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
            logger.debug(f"read_heartbeats: {msg_count} messages in {elapsed:.3f}s")

        # Run all three reads in parallel for faster collection
        await asyncio.gather(
            read_registry(),
            read_status(),
            read_heartbeats()
        )

        # Calculate uptime for running services
        now = datetime.now(timezone.utc)
        for service in services.values():
            if service.start_time and not service.stop_time:
                service.uptime_seconds = (now - service.start_time).total_seconds()

        return list(services.values())

    async def _follow_registry(self):
        """Continuously follow registry events."""
        try:
            registry_reader = MsgReader(
                subject=f"{self.subject_prefix}.registry.>",
                parent=self.messenger,
                deliver_policy="last_per_subject",  # Start from most recent per service
            )
            async with registry_reader:
                async for data, meta in registry_reader:
                    if not self._following:
                        break

                    event = data.get('event')
                    service_id = data.get('service_id')

                    if not service_id:
                        continue

                    # Initialize service info if new
                    if service_id not in self._services:
                        self._services[service_id] = ServiceInfo(
                            service_id=service_id,
                            status=Status.UNKNOWN
                        )

                    service = self._services[service_id]

                    # Update based on event type
                    if event == 'start':
                        timestamp = data.get('timestamp')
                        if timestamp:
                            service.start_time = dt_from_array(timestamp)
                            service.stop_time = None

                        # Extract metadata
                        if 'runner_id' in data:
                            service.runner_id = data['runner_id']
                        if 'hostname' in data:
                            service.hostname = data['hostname']
                        if 'pid' in data:
                            service.pid = data['pid']

                        # Calculate uptime
                        if service.start_time:
                            service.uptime_seconds = (datetime.now(timezone.utc) - service.start_time).total_seconds()

                        # Trigger callback
                        if self.on_service_start:
                            self.on_service_start(service)
                        if self.on_service_update:
                            self.on_service_update(service)

                    elif event == 'stop':
                        timestamp = data.get('timestamp')
                        if timestamp:
                            service.stop_time = dt_from_array(timestamp)

                        # Trigger callback
                        if self.on_service_stop:
                            self.on_service_stop(service)
                        if self.on_service_update:
                            self.on_service_update(service)

        except asyncio.CancelledError:
            logger.debug("Registry following cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in registry following: {e}")

    async def _follow_status(self):
        """Continuously follow status updates."""
        try:
            status_reader = MsgReader(
                subject=f'{self.subject_prefix}.status.>',
                parent=self.messenger,
                deliver_policy='last_per_subject',  # Start from most recent per service
            )
            async with status_reader:
                async for data, meta in status_reader:
                    if not self._following:
                        break

                    service_id = data.get('name')
                    if not service_id:
                        continue

                    # Initialize if unknown service
                    if service_id not in self._services:
                        self._services[service_id] = ServiceInfo(
                            service_id=service_id,
                            status=Status.UNKNOWN
                        )

                    service = self._services[service_id]

                    # Update status
                    status_str = data.get('status')
                    if status_str:
                        service.status = Status(status_str)
                        service.status_message = data.get('message')

                        # Capture timestamp
                        if 'timestamp' in meta:
                            ts = meta['timestamp']
                            if isinstance(ts, list):
                                service.last_status_update = dt_from_array(ts)

                        # Trigger callback
                        if self.on_service_update:
                            self.on_service_update(service)

        except asyncio.CancelledError:
            logger.debug("Status following cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in status following: {e}")

    async def _follow_heartbeats(self):
        """Continuously follow heartbeat updates."""
        try:
            heartbeat_reader = MsgReader(
                subject=f"{self.subject_prefix}.heartbeat.>",
                parent=self.messenger,
                deliver_policy="last_per_subject",  # Start from most recent per service
            )
            async with heartbeat_reader:
                async for data, meta in heartbeat_reader:
                    if not self._following:
                        break

                    service_id = data.get('service_id')
                    if not service_id or service_id not in self._services:
                        continue

                    service = self._services[service_id]

                    # Update heartbeat
                    timestamp = data.get('timestamp')
                    if timestamp:
                        service.last_heartbeat = dt_from_array(timestamp)

                        # Update uptime if running
                        if service.start_time and not service.stop_time:
                            service.uptime_seconds = (datetime.now(timezone.utc) - service.start_time).total_seconds()

                        # Trigger callback (heartbeat updates don't need separate callback)
                        if self.on_service_update:
                            self.on_service_update(service)

        except asyncio.CancelledError:
            logger.debug("Heartbeat following cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in heartbeat following: {e}")
