"""Base classes for service launchers and runners.

According to the architecture:
- ServiceRunner: Controls service lifetime from launcher process
- ServicesLauncher: Manages collection of ServiceRunners from config
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ocabox_tcs.monitoring.monitored_object import MonitoredObject


@dataclass
class ServiceRunnerConfig:
    """Configuration for a service runner."""
    service_type: str
    instance_context: str | None = None
    config_file: str | None = None
    runner_id: str | None = None
    module: str | None = None  # Optional: full module path for external packages

    @property
    def service_id(self) -> str:
        """Get service identifier."""
        if self.instance_context:
            return f"{self.service_type}-{self.instance_context}"
        return self.service_type


class BaseRunner(ABC):
    """Base class for service runners.

    ServiceRunner controls service lifetime from launcher process.
    Exists for all services (running, stopped, periodic).
    Specialized subclasses handle different execution methods.
    """

    def __init__(self, config: ServiceRunnerConfig):
        self.config = config
        self.logger = logging.getLogger(f"run.{self.config.service_id}")
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._is_running

    @property
    def service_id(self) -> str:
        """Get service identifier."""
        return self.config.service_id

    @abstractmethod
    async def start(self) -> bool:
        """Start the service.

        Returns:
            True if service started successfully, False otherwise
        """
        pass

    @abstractmethod
    async def stop(self) -> bool:
        """Stop the service.

        Returns:
            True if service stopped successfully, False otherwise
        """
        pass

    @abstractmethod
    async def restart(self) -> bool:
        """Restart the service.

        Returns:
            True if service restarted successfully, False otherwise
        """
        pass

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        """Get service status information.

        Returns:
            Dictionary containing service status details
        """
        pass


class BaseLauncher(ABC):
    """Base class for service launchers.

    ServicesLauncher manages collection of ServiceRunners from config.
    Maintains launching system and delegates start/stop to runners.

    Launchers use MessengerMonitoredObject for self-monitoring:
    - Publish status updates when launcher state changes
    - Send heartbeats to indicate launcher health
    - Services can reference launcher as parent for hierarchical display
    """

    def __init__(self, launcher_id: str = "launcher"):
        self.launcher_id = launcher_id
        self.logger = logging.getLogger(f"launch.{launcher_id}")
        self.runners: dict[str, BaseRunner] = {}
        self.monitor: "MonitoredObject | None" = None

    @abstractmethod
    async def initialize(self, config: Any) -> bool:
        """Initialize launcher with configuration.

        Args:
            config: Launcher configuration (format depends on launcher type)

        Returns:
            True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    async def start_all(self) -> bool:
        """Start all configured services.

        Returns:
            True if all services started successfully, False otherwise
        """
        pass

    @abstractmethod
    async def stop_all(self) -> bool:
        """Stop all running services.

        Returns:
            True if all services stopped successfully, False otherwise
        """
        pass

    async def start_service(self, service_id: str) -> bool:
        """Start specific service by ID.

        Args:
            service_id: Service identifier

        Returns:
            True if service started successfully, False otherwise
        """
        if service_id not in self.runners:
            self.logger.error(f"Service {service_id} not found")
            return False
        return await self.runners[service_id].start()

    async def stop_service(self, service_id: str) -> bool:
        """Stop specific service by ID.

        Args:
            service_id: Service identifier

        Returns:
            True if service stopped successfully, False otherwise
        """
        if service_id not in self.runners:
            self.logger.error(f"Service {service_id} not found")
            return False
        return await self.runners[service_id].stop()

    async def get_status(self) -> dict[str, Any]:
        """Get status of all services.

        Returns:
            Dictionary mapping service IDs to their status
        """
        status = {}
        for service_id, runner in self.runners.items():
            status[service_id] = await runner.get_status()
        return status

    async def initialize_monitoring(self, monitor_name: str | None = None,
                                   subject_prefix: str = "svc"):
        """Initialize launcher monitoring.

        Args:
            monitor_name: Optional custom name (default: "launcher.{launcher_id}")
            subject_prefix: NATS subject prefix (default: "svc")
        """
        from ocabox_tcs.monitoring import create_monitor, Status

        if self.monitor is not None:
            self.logger.warning("Monitoring already initialized")
            return

        name = monitor_name or f"launcher.{self.launcher_id}"
        self.monitor = await create_monitor(
            name=name,
            heartbeat_interval=10.0,
            healthcheck_interval=30.0,
            subject_prefix=subject_prefix
        )

        # Set initial status
        self.monitor.set_status(Status.STARTUP, "Launcher initializing")
        self.logger.info(f"Initialized monitoring as '{name}'")

        # Warn if monitoring is disabled (no NATS connection)
        from ocabox_tcs.monitoring.monitored_object import DummyMonitoredObject
        if isinstance(self.monitor, DummyMonitoredObject):
            self.logger.warning("=" * 60)
            self.logger.warning("Launcher monitoring DISABLED - no NATS connection")
            self.logger.warning("Launcher will NOT appear in tcsctl")
            self.logger.warning("=" * 60)

    async def start_monitoring(self):
        """Start launcher monitoring (heartbeats and status updates)."""
        if self.monitor is None:
            self.logger.warning("Monitoring not initialized, cannot start")
            return

        from ocabox_tcs.monitoring import Status

        # Send registration (no-op for DummyMonitoredObject)
        await self.monitor.send_registration()

        await self.monitor.start_monitoring()
        self.monitor.set_status(Status.OK, "Launcher running")
        self.logger.info("Launcher monitoring started")

    async def stop_monitoring(self):
        """Stop launcher monitoring."""
        if self.monitor is None:
            return

        from ocabox_tcs.monitoring import Status

        self.monitor.set_status(Status.SHUTDOWN, "Launcher shutting down")
        await self.monitor.stop_monitoring()

        # Send shutdown (no-op for DummyMonitoredObject)
        await self.monitor.send_shutdown()

        self.logger.info("Launcher monitoring stopped")
