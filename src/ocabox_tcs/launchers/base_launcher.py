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
    from ocabox_tcs.management.process_context import ProcessContext
    from ocabox_tcs.management.service_registry import ServiceRegistry


@dataclass
class ServiceRunnerConfig:
    """Configuration for a service runner.

    Attributes:
        service_type: Service type identifier (from @service decorator)
        variant: Instance variant identifier (cannot contain dots)
        config_file: Optional path to config file
        runner_id: Optional runner ID for tracking
        parent_name: Optional parent entity name for hierarchical display

        Restart policy fields (systemd-inspired):
        restart: Policy - 'no', 'always', 'on-failure', 'on-abnormal'
        restart_sec: Delay before restart (seconds)
        restart_max: Max restarts in window (0 = unlimited)
        restart_window: Time window for restart counting (seconds)
    """
    service_type: str
    variant: str = "dev"  # Default variant
    config_file: str | None = None
    runner_id: str | None = None
    parent_name: str | None = None  # For hierarchical display

    # Restart policy fields
    restart: str = "no"  # Options: no, always, on-failure, on-abnormal
    restart_sec: float = 5.0  # Delay before restart (seconds)
    restart_max: int = 0  # Max restarts in window (0 = unlimited)
    restart_window: float = 60.0  # Time window for restart counting (seconds)

    @property
    def service_id(self) -> str:
        """Get full service identifier in format '{type}.{variant}'."""
        return f"{self.service_type}.{self.variant}"


class BaseRunner(ABC):
    """Base class for service runners.

    ServiceRunner controls service lifetime from launcher process.
    Exists for all services (running, stopped, periodic).
    Specialized subclasses handle different execution methods.
    """

    def __init__(
        self,
        config: ServiceRunnerConfig,
        launcher_id: str | None = None,
        subject_prefix: str = "svc"
    ):
        self.config = config
        self.launcher_id = launcher_id  # Direct reference to parent launcher (not parsed!)
        self.subject_prefix = subject_prefix  # NATS subject prefix
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

    def _get_full_service_id(self) -> str:
        """Get full service ID in format '{type}.{variant}'.

        Returns:
            Service ID string (e.g., 'hello_world.dev', 'halina.server.prod')
        """
        return self.config.service_id

    async def _publish_registry_event(self, event: str, **extra_data):
        """Universal method to publish registry events to NATS.

        Handles all common logic: ProcessContext, messenger, service_id construction.
        No more code duplication!

        Args:
            event: Event type ('start', 'stop', 'declared', 'crashed', etc.)
            **extra_data: Additional fields for the event data dict
        """
        # Skip if no runner_id (standalone/test mode - no lifecycle pollution)
        if not self.config.runner_id:
            self.logger.debug(f"No runner_id, skipping {event.upper()} event for {self.service_id}")
            return

        try:
            from serverish.messenger import single_publish
            from serverish.base import dt_utcnow_array
            from ocabox_tcs.management.process_context import ProcessContext

            # Get messenger from ProcessContext
            process_ctx = ProcessContext()
            if process_ctx is None or process_ctx.messenger is None:
                self.logger.debug(f"No NATS messenger available, cannot publish {event.upper()} event")
                return

            # Construct full service_id
            service_id = self._get_full_service_id()

            # Build subject
            subject = f"{self.subject_prefix}.registry.{event}.{service_id}"

            # Build base data (always include these)
            data = {
                "event": event,
                "service_id": service_id,
                "timestamp": dt_utcnow_array(),
            }

            # Add extra fields
            data.update(extra_data)

            # Add parent for hierarchical grouping if launcher_id is set
            if self.launcher_id and "parent" not in data:
                data["parent"] = f"launcher.{self.launcher_id}"

            # Add runner_id if not already present
            if "runner_id" not in data and self.config.runner_id:
                data["runner_id"] = self.config.runner_id

            await single_publish(subject, data)
            self.logger.info(f"Published {event.upper()} event for {service_id}")

        except Exception as e:
            self.logger.error(f"Failed to publish {event.upper()} event for {self.service_id}: {e}")

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

    async def _publish_start_event(self, pid: int | None = None):
        """Publish START event to NATS registry.

        Called when service starts successfully. Runner owns lifecycle events.

        Args:
            pid: Process ID (for subprocess launchers, None for asyncio)
        """
        import socket
        import os

        data = {
            "status": "startup",
            "hostname": socket.gethostname()
        }

        if pid is not None:
            data["pid"] = pid
        else:
            data["pid"] = os.getpid()

        await self._publish_registry_event("start", **data)

    async def _publish_stop_event(self, reason: str = "completed", exit_code: int = 0):
        """Publish STOP event to NATS registry.

        Called when service stops cleanly or is force-killed by launcher.

        Args:
            reason: Reason for stop (e.g., "completed", "force_killed")
            exit_code: Process exit code
        """
        await self._publish_registry_event(
            "stop",
            status="shutdown",
            reason=reason,
            exit_code=exit_code
        )

    async def _publish_crash_event(self, exit_code: int):
        """Publish CRASH event to NATS registry.

        Args:
            exit_code: Process exit code
        """
        will_restart = self._should_restart(exit_code)
        await self._publish_registry_event(
            "crashed",
            status="error" if will_restart else "failed",
            exit_code=exit_code,
            restart_policy=self.config.restart,
            will_restart=will_restart
        )

    async def _publish_restarting_event(self, attempt: int):
        """Publish RESTARTING event to NATS registry.

        Args:
            attempt: Restart attempt number (1-based)
        """
        await self._publish_registry_event(
            "restarting",
            status="startup",
            restart_attempt=attempt,
            max_restarts=self.config.restart_max if self.config.restart_max > 0 else None
        )

    async def _publish_failed_event(self, reason: str):
        """Publish FAILED event to NATS registry.

        Args:
            reason: Reason for failure (e.g., 'restart_failed', 'restart_limit_reached')
        """
        await self._publish_registry_event(
            "failed",
            status="failed",
            reason=reason,
            restart_count=len(self._restart_history)
        )

    async def publish_declared(self):
        """Publish DECLARED event to NATS registry.

        Called by launcher after runner creation. Only publishes if runner_id
        is present (skips for ad-hoc standalone/test runs).

        This marks the service as part of the launcher's formal configuration,
        distinguishing it from ephemeral services.
        """
        await self._publish_registry_event(
            "declared",
            restart_policy=self.config.restart,
            # Note: parent and runner_id are added automatically by _publish_registry_event
        )


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
        self._shutdown_event = None  # Will be initialized as asyncio.Event() when needed
        self.process_ctx: Any | None = None
        self.cli_args: Any | None = None  # Parsed CLI arguments namespace

    @staticmethod
    def prepare_cli_argument_parser() -> "argparse.ArgumentParser":
        """Create and return ArgumentParser with common launcher options.

        This static method creates a parser with arguments common to all launchers.
        Subclasses should call this, then customize the parser before parsing.

        Returns:
            ArgumentParser with common options configured
        """
        import argparse

        parser = argparse.ArgumentParser(add_help=False)  # Don't add help yet (subclass will)

        # Common arguments for all launchers
        parser.add_argument(
            "--config",
            default=None,
            help="Path to services config file (default: config/services.yaml)"
        )
        parser.add_argument(
            "--no-banner",
            action="store_true",
            help="Suppress startup banner"
        )
        parser.add_argument(
            "--no-color",
            action="store_true",
            help="Disable colored logging (use plain text)"
        )

        return parser

    @staticmethod
    def setup_logging(use_color: bool):
        """Setup logging based on color preference.

        Args:
            use_color: If True, use Rich colored logging; if False, use plain text
        """
        import logging

        if not use_color:
            # Plain text logging
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-15s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        else:
            # Try Rich colored logging, fall back to plain if not available
            try:
                from rich.logging import RichHandler
                logging.basicConfig(
                    level=logging.INFO,
                    format='%(message)s',
                    handlers=[RichHandler(
                        show_time=True,
                        show_level=True,
                        show_path=False,
                        rich_tracebacks=True,
                        tracebacks_show_locals=False,
                        log_time_format='%Y-%m-%d %H:%M:%S'
                    )]
                )
            except ImportError:
                # Rich not available, fall back to plain text
                logging.basicConfig(
                    level=logging.INFO,
                    format='%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-15s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S'
                )

    @staticmethod
    def determine_config_file(config_arg: str | None) -> str:
        """Determine and validate config file from argument.

        Args:
            config_arg: Config file path from CLI argument (or None for default)

        Returns:
            Path to config file

        Raises:
            SystemExit: If explicitly provided config file doesn't exist
        """
        import sys
        import logging
        from pathlib import Path

        logger = logging.getLogger("launch")

        if config_arg is not None:
            # User explicitly provided --config, file MUST exist
            config_file = config_arg
            if not Path(config_file).exists():
                logger.error(f"Configuration file not found: {config_file}")
                logger.error("Explicitly provided config file must exist. Exiting.")
                sys.exit(1)
            logger.info(f"Using config file: {config_file}")
        else:
            # Use default, missing file is OK (will use defaults)
            config_file = "config/services.yaml"
            if not Path(config_file).exists():
                logger.info(f"Default config file not found: {config_file}")
                logger.info("Continuing with empty configuration")
            else:
                logger.info(f"Using default config file: {config_file}")

        return config_file

    @classmethod
    async def launch(cls, launcher_factory, parser_customizer=None):
        """Common launcher orchestration for all entry points.

        This method handles all common startup logic: environment loading, argument
        parsing, logging setup, config validation, and launcher lifecycle orchestration.

        Args:
            launcher_factory: Callable(launcher_id, args) -> BaseLauncher
                Factory function that creates the launcher instance
            parser_customizer: Optional Callable(parser) -> parser
                Function to customize the parser (add launcher-specific args, set description, etc.)
        """
        import logging
        import os
        import socket
        from ocabox_tcs.management.environment import load_dotenv_if_available
        from ocabox_tcs.management.process_context import ProcessContext

        # Load .env file if it exists
        env_loaded, env_file_path = load_dotenv_if_available()

        # Prepare parser with common arguments
        parser = cls.prepare_cli_argument_parser()

        # Allow customization (description, launcher-specific args, epilog, etc.)
        if parser_customizer:
            parser = parser_customizer(parser)

        # Parse arguments
        args = parser.parse_args()

        # Setup logging based on --no-color flag
        cls.setup_logging(use_color=not args.no_color)

        logger = logging.getLogger("launch")

        # Log if .env was loaded
        if env_loaded and env_file_path:
            logger.info(f"Loaded environment from {env_file_path}")

        # Determine and validate config file from --config argument
        config_file = cls.determine_config_file(args.config)

        # Initialize ProcessContext
        process_ctx = await ProcessContext.initialize(config_file=config_file)

        # Generate launcher ID (will be overridden by factory with correct launcher type)
        launcher_id = cls.gen_launcher_name(
            "launcher",  # Generic, will be set correctly by factory
            config_file,
            os.getcwd(),
            socket.gethostname()
        )

        # Create launcher via factory
        launcher = launcher_factory(launcher_id, args)

        # Store CLI arguments in launcher
        launcher.cli_args = args

        # Print startup banner (unless suppressed by --no-banner)
        if not args.no_banner:
            logger.info("=" * 60)
            logger.info("TCS - Telescope Control Services")
            logger.info(f"Launcher: {launcher._get_launcher_type_display()}")
            logger.info("=" * 60)

        # Initialize, start, run
        if not await launcher.initialize(process_ctx):
            logger.error("Failed to initialize launcher")
            await process_ctx.shutdown()
            return

        if not await launcher.start_all():
            logger.error("Failed to start services")
            await launcher.stop_all()
            await process_ctx.shutdown()
            return

        await launcher.run()

    def _get_launcher_type_display(self) -> str:
        """Get display name for banner.

        Override in subclasses to customize banner text.

        Returns:
            Human-readable launcher type description
        """
        return self.__class__.__name__

    async def initialize(self, process_ctx: "ProcessContext") -> bool:
        """Template method for launcher initialization.

        Orchestrates common initialization flow, delegates runner creation to subclass.

        Args:
            process_ctx: Already-initialized ProcessContext

        Returns:
            True if initialization successful, False otherwise
        """
        from ocabox_tcs.management.service_registry import ServiceRegistry

        try:
            # Store ProcessContext reference
            self.process_ctx = process_ctx
            self.logger.debug(f"Using ProcessContext for {self.__class__.__name__}")

            # Extract subject prefix from NATS config
            subject_prefix = 'svc'  # Default
            if process_ctx.config_manager:
                global_config = process_ctx.config_manager.resolve_config()
                nats_config = global_config.get("nats", {})
                subject_prefix = nats_config.get("subject_prefix", "svc")
                self.logger.debug(f"Using NATS subject prefix: {subject_prefix}")

            # Store subject_prefix for runners to use
            self.subject_prefix = subject_prefix

            # Initialize launcher monitoring (auto-detects NATS via ProcessContext)
            await self.initialize_monitoring(subject_prefix=subject_prefix)

            # Get raw config for services list and registry
            raw_config = process_ctx.config_manager.get_raw_config()
            services_list = raw_config.get('services', [])

            if not services_list:
                self.logger.warning("No services found in configuration")
                return True

            # Create ServiceRegistry from config
            registry = ServiceRegistry(raw_config)

            # Register runners for each service
            for service_cfg in services_list:
                service_type = service_cfg['type']
                # Support both old 'instance_context' and new 'variant' field names
                variant = service_cfg.get('variant') or service_cfg.get('instance_context', 'dev')

                runner_config = ServiceRunnerConfig(
                    service_type=service_type,
                    variant=variant,
                    config_file=process_ctx.config_file,
                    runner_id=f"{self.launcher_id}.{service_type}",
                    parent_name=f"launcher.{self.launcher_id}",
                    restart=service_cfg.get('restart', 'no'),
                    restart_sec=float(service_cfg.get('restart_sec', 5.0)),
                    restart_max=int(service_cfg.get('restart_max', 0)),
                    restart_window=float(service_cfg.get('restart_window', 60.0))
                )

                # HOOK: Subclass creates appropriate runner type
                runner = self._create_runner(runner_config, registry, subject_prefix)

                self.runners[runner.service_id] = runner
                self.logger.debug(f"Registered runner for {runner.service_id}")
                self.logger.debug(
                    f"Restart policy for {runner.service_id}: {runner_config.restart} "
                    f"(max={runner_config.restart_max}, delay={runner_config.restart_sec}s)"
                )

            # Declare services to registry (marks them as part of configuration)
            await self.declare_services(subject_prefix=subject_prefix)

            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize launcher: {e}", exc_info=True)
            return False

    @abstractmethod
    def _create_runner(
        self,
        config: ServiceRunnerConfig,
        registry: "ServiceRegistry",
        subject_prefix: str
    ) -> BaseRunner:
        """Hook for subclasses to create launcher-specific runner type.

        Args:
            config: Runner configuration
            registry: ServiceRegistry for module resolution
            subject_prefix: NATS subject prefix

        Returns:
            Appropriate runner instance (ProcessRunner, AsyncioRunner, etc.)
        """
        pass

    async def start_all(self) -> bool:
        """Start all configured services.

        Returns:
            True if all services started successfully, False otherwise
        """
        success = True
        for service_id, runner in self.runners.items():
            self.logger.info(f"Starting service: {service_id}")
            if not await runner.start():
                self.logger.error(f"Failed to start {service_id}")
                success = False
            else:
                self.logger.info(f"Service started: {service_id}")

        # Start launcher monitoring after services are started
        if success:
            await self.start_monitoring()

        return success

    async def stop_all(self) -> bool:
        """Stop all running services in parallel.

        Returns:
            True if all services stopped successfully, False otherwise
        """
        import asyncio

        if not self.runners:
            return True

        # Stop all services in parallel for faster shutdown
        results = await asyncio.gather(
            *[self.stop_service(sid) for sid in self.runners.keys()],
            return_exceptions=True
        )

        # Check if any failed
        success = True
        for sid, result in zip(self.runners.keys(), results):
            if isinstance(result, Exception):
                self.logger.error(f"Failed to stop {sid}: {result}")
                success = False
            elif not result:
                self.logger.error(f"Failed to stop {sid}")
                success = False

        return success

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

    async def run(self):
        """Run launcher with signal handling."""
        import asyncio
        import signal

        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()

        def handle_signal(sig):
            self.logger.info(f"Received signal {sig}, shutting down...")
            asyncio.create_task(self._shutdown())

        loop.add_signal_handler(signal.SIGINT, lambda: handle_signal("SIGINT"))
        loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal("SIGTERM"))

        self.logger.info("Services started. Press Ctrl+C to stop.")
        await self._shutdown_event.wait()
        self.logger.info("Launcher shutdown complete")

    async def _shutdown(self):
        """Shutdown all services and process context."""
        # Stop launcher monitoring first
        await self.stop_monitoring()

        self.logger.info("Stopping all services...")
        await self.stop_all()

        if self.process_ctx:
            await self.process_ctx.shutdown()

        if self._shutdown_event:
            self._shutdown_event.set()
