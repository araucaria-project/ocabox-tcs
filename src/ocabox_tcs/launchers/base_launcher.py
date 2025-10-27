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

    # Restart policy fields
    restart: str = "no"  # Options: no, always, on-failure, on-abnormal
    restart_sec: float = 5.0  # Delay before restart (seconds)
    restart_max: int = 0  # Max restarts in window (0 = unlimited)
    restart_window: float = 60.0  # Time window for restart counting (seconds)

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
        self.logger = logging.getLogger(f"run|{self.config.service_id.rsplit('.', 1)[-1]})")
        self._is_running = False
        self._restart_count = 0
        self._restart_history: list[float] = []
        self._last_crash_time: float | None = None

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

    def _should_restart(self, exit_code: int) -> bool:
        """Determine if service should be restarted based on policy.

        Args:
            exit_code: Process exit code (0 = success, non-zero = failure)

        Returns:
            True if service should be restarted
        """
        policy = self.config.restart

        # Check restart limit
        if self.config.restart_max > 0:
            self._cleanup_restart_history()
            if self._restart_count >= self.config.restart_max:
                self.logger.warning(
                    f"Restart limit reached ({self.config.restart_max} restarts "
                    f"in {self.config.restart_window}s), giving up"
                )
                return False

        # Apply restart policy
        if policy == "no":
            return False
        elif policy == "always":
            return True
        elif policy == "on-failure":
            # Restart on non-zero exit code
            return exit_code != 0
        elif policy == "on-abnormal":
            # Restart on crash/signal (exit code > 128 or < 0)
            return exit_code > 128 or exit_code < 0
        else:
            self.logger.warning(f"Unknown restart policy: {policy}, not restarting")
            return False

    def _cleanup_restart_history(self):
        """Remove restart timestamps outside the restart window."""
        from time import time
        now = time()
        cutoff = now - self.config.restart_window
        self._restart_history = [
            ts for ts in self._restart_history if ts > cutoff
        ]
        self._restart_count = len(self._restart_history)


class BaseLauncher(ABC):
    """Base class for service launchers.

    ServicesLauncher manages collection of ServiceRunners from config.
    Maintains launching system and delegates start/stop to runners.

    Launchers use MessengerMonitoredObject for self-monitoring:
    - Publish status updates when launcher state changes
    - Send heartbeats to indicate launcher health
    - Services can reference launcher as parent for hierarchical display
    """

    @staticmethod
    def gen_launcher_name(launcher_type: str, *unique_keys) -> str:
        """Generate deterministic launcher name from unique keys.

        Creates a stable launcher ID based on launcher type, hostname, and
        configurable unique keys (e.g., config file path, working directory).
        Same inputs always produce same ID for idempotency.

        Args:
            launcher_type: Launcher type prefix (e.g., "process-launcher")
            *unique_keys: Variable args to create hash for uniqueness
                         (e.g., config_file, pwd, hostname)

        Returns:
            Launcher name in format "launcher.hash6chars.hostname-launcher_type"
            Example: "launcher.Awq6fK.majkma-process-launcher"

            This format ensures shortening (taking last component after last dot)
            produces a descriptive name: "majkma-process-launcher"
        """
        import socket
        import hashlib
        import base64

        hostname_short = socket.gethostname().split('.')[0]

        # Create hash from all unique keys concatenated
        combined = "|".join(str(k) for k in unique_keys if k)
        hash_bytes = hashlib.sha256(combined.encode()).digest()

        # Encode as base62-like using base64 (0-9a-zA-Z-_)
        # First 6 chars provide ~36 bits of uniqueness
        key_hash = base64.urlsafe_b64encode(hash_bytes).decode()[:6]
        # Replace URL-safe chars with letters for better readability
        key_hash = key_hash.replace('-', 'x').replace('_', 'y')

        return f"launcher.{key_hash}.{hostname_short}-{launcher_type}"

    def __init__(self, launcher_id: str = "launcher"):
        self.launcher_id = launcher_id
        self.logger = logging.getLogger(f"lch|{launcher_id}")
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

    async def declare_services(self, subject_prefix: str = "svc"):
        """Publish declared events for all configured services.

        Delegates to each runner's publish_declared() method. Runners only
        publish if they have a runner_id (skips standalone/test services).

        This should be called after services are registered from configuration.
        Declared messages mark services as part of the formal configuration,
        distinguishing them from ad hoc ephemeral services.

        Args:
            subject_prefix: NATS subject prefix (default: "svc")
        """
        # Delegate to each runner - they decide whether to publish based on runner_id
        declared_count = 0
        for runner in self.runners.values():
            # Check if runner has publish_declared method (not all runner types may implement it)
            if hasattr(runner, 'publish_declared'):
                await runner.publish_declared()
                # Count only if runner_id present (runner will skip otherwise)
                if runner.config.runner_id:
                    declared_count += 1

        if declared_count > 0:
            self.logger.info(f"Declared {declared_count} services to registry")

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
