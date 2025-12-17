"""ServiceController for managing individual service instances."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from ocabox_tcs.management.configuration import ConfigurationManager, EnvConfigSource
from ocabox_tcs.management.process_context import ProcessContext
from ocabox_tcs.management.service_registry import (
    ServiceRegistry,
    build_service_id,
    parse_service_id,
    validate_variant,
)

from ocabox_tcs.base_service import BaseService, BaseServiceConfig, get_config_class
from ocabox_tcs.monitoring import create_monitor, Status
from ocabox_tcs.monitoring.monitored_object import MonitoredObject


class ServiceController:
    """Controls single service in same process as service.

    The ServiceController manages a single service instance, handling:
    - Service class discovery via ServiceRegistry
    - Configuration loading and merging
    - Service lifecycle (start, stop, restart)
    - Monitoring (status, heartbeats)

    Attributes:
        service_type: Service type identifier (e.g., 'hello_world', 'halina.server')
        variant: Instance variant identifier (e.g., 'dev', 'prod')
        service_id: Full identifier in format '{type}.{variant}'
    """

    def __init__(
        self,
        service_type: str,
        variant: str,
        registry: ServiceRegistry | None = None,
        runner_id: str | None = None,
        parent_name: str | None = None,
    ):
        """Initialize the ServiceController.

        Args:
            service_type: Service type identifier (from @service decorator)
            variant: Instance variant (cannot contain dots)
            registry: Optional ServiceRegistry for class discovery
            runner_id: Optional runner ID for tracking (from launcher)
            parent_name: Optional parent entity name for hierarchical display
        """
        # Validate variant has no dots
        validate_variant(variant)

        self.service_type = service_type
        self.variant = variant
        self.runner_id = runner_id
        self.parent_name = parent_name
        self.service_id = build_service_id(service_type, variant)

        self.logger = logging.getLogger(f"ctl|{self.service_id}")
        self.process = ProcessContext()

        # Service registry for class discovery
        self._registry = registry

        # Initialize monitoring
        self.monitor: MonitoredObject | None = None

        # Service management
        self._service: BaseService | None = None
        self._service_class: type[BaseService] | None = None
        self._config_class: type[BaseServiceConfig] | None = None
        self._config: BaseServiceConfig | None = None
        self._config_manager: ConfigurationManager | None = None

        # State
        self._initialized = False
        self._running = False

        # Register with process
        self.process.register_controller(self)

        self.logger.debug(f"Created controller for {self.service_id}")

    @classmethod
    def from_service_id(
        cls,
        service_id: str,
        registry: ServiceRegistry | None = None,
        runner_id: str | None = None,
        parent_name: str | None = None,
    ) -> "ServiceController":
        """Create a ServiceController from a service_id string.

        Args:
            service_id: Full identifier in format '{type}.{variant}'
            registry: Optional ServiceRegistry for class discovery
            runner_id: Optional runner ID for tracking
            parent_name: Optional parent entity name

        Returns:
            New ServiceController instance
        """
        service_type, variant = parse_service_id(service_id)
        return cls(
            service_type=service_type,
            variant=variant,
            registry=registry,
            runner_id=runner_id,
            parent_name=parent_name,
        )

    async def initialize(self) -> bool:
        """Initialize the controller and discover service classes.

        Note: ProcessContext must already be initialized before calling this.
        """
        if self._initialized:
            return True

        try:
            # Create registry from config if not provided
            if self._registry is None and self.process.config_manager:
                registry_config = self.process.config_manager.get_registry()
                self._registry = ServiceRegistry({"registry": registry_config})

            # Discover service and config classes
            if not await self._discover_classes():
                return False

            # Setup configuration (uses ProcessContext.config_manager)
            await self._setup_configuration()

            # Initialize monitoring
            await self._initialize_monitoring()
            if self.monitor:
                self.monitor.set_status(Status.STARTUP, "Initializing controller")

            self._initialized = True
            if self.monitor:
                self.monitor.set_status(Status.OK, "Controller initialized")
            self.logger.debug("Controller initialized successfully")
            return True

        except Exception as e:
            error_msg = f"Failed to initialize controller: {e}"
            self.logger.error(error_msg)
            if self.monitor:
                self.monitor.set_status(Status.FAILED, error_msg)
            return False

    async def start_service(self) -> bool:
        """Create and start the service."""
        if not self._initialized:
            self.logger.error("Controller not initialized, cannot start service")
            return False

        if self._running:
            self.logger.warning("Service already running")
            return True

        try:
            self.monitor.set_status(Status.STARTUP, "Starting service")

            # Create service instance
            if not await self._create_service():
                self.monitor.set_status(Status.FAILED, "Failed to create service")
                return False

            # Start the service
            await self._service._internal_start()

            self._running = True
            self.monitor.set_status(Status.OK, "Service running")
            self.logger.info("Service started successfully")
            return True

        except Exception as e:
            error_msg = f"Failed to start service: {e}"
            self.logger.error(error_msg)
            self.monitor.set_status(Status.FAILED, error_msg)
            return False

    async def stop_service(self) -> bool:
        """Stop the service."""
        if not self._running:
            return True

        try:
            self.monitor.set_status(Status.SHUTDOWN, "Stopping service")

            if self._service:
                await self._service._internal_stop()

            self._running = False
            self.monitor.set_status(Status.OK, "Service stopped")
            self.logger.info("Service stopped successfully")
            return True

        except Exception as e:
            error_msg = f"Failed to stop service: {e}"
            self.logger.error(error_msg)
            self.monitor.set_status(Status.ERROR, error_msg)
            return False

    async def restart_service(self) -> bool:
        """Restart the service."""
        self.logger.info("Restarting service")
        await self.stop_service()
        return await self.start_service()

    async def shutdown(self):
        """Shutdown the controller completely."""
        self.logger.info("Shutting down controller")

        # Stop service if running
        if self._running:
            await self.stop_service()

        # Stop monitoring (sets status to reflect shutdown reason)
        # Note: No registry.stop event here - runner handles lifecycle events
        if self.monitor:
            await self.monitor.stop_monitoring()

        # Unregister from process
        self.process.unregister_controller(self)

        self.logger.info("Controller shutdown complete")

    async def _initialize_monitoring(self):
        """Initialize monitoring system.

        Creates monitor for status updates and heartbeats.
        Registry/lifecycle events are handled by runners (if launcher-managed).
        """
        # Get subject_prefix from NATS config
        subject_prefix = 'svc'  # Default
        if self.process.config_manager:
            global_config = self.process.config_manager.resolve_config()
            nats_config = global_config.get("nats", {})
            subject_prefix = nats_config.get("subject_prefix", "svc")
            self.logger.debug(f"Using NATS subject prefix: {subject_prefix}")

        # Create monitor using factory (auto-detects NATS availability)
        # Monitor publishes: status updates + heartbeats (NO registry events)
        self.monitor = await create_monitor(
            name=self.service_id,
            heartbeat_interval=10.0,
            healthcheck_interval=30.0,
            parent_name=self.parent_name,  # For hierarchical grouping in displays
            subject_prefix=subject_prefix  # Use configured prefix
        )

        # Set initial status BEFORE starting monitoring (to avoid initial "unknown" report)
        self.monitor.set_status(Status.STARTUP, "Initializing monitoring")

        # Start monitoring (heartbeats + status updates)
        await self.monitor.start_monitoring()

        self.logger.debug("Monitoring initialized")

    async def _discover_classes(self) -> bool:
        """Discover service and config classes.

        Discovery order:
        1. Check decorator registry first (for already-imported classes)
        2. Use ServiceRegistry to import and find class (for launcher-managed services)
        """
        try:
            from ocabox_tcs.base_service import get_service_class

            # First, check if class is already registered via decorator
            # This handles standalone services that were imported before controller creation
            self._service_class = get_service_class(self.service_type)

            if self._service_class is not None:
                self.logger.debug(
                    f"Service class found in decorator registry: {self._service_class.__name__}"
                )
            elif self._registry:
                # Class not in decorator registry, try to import via ServiceRegistry
                self._service_class = self._registry.get_service_class(self.service_type)
                self.logger.debug(
                    f"Service class discovered via ServiceRegistry: {self._service_class.__name__}"
                )
            else:
                self.logger.error(
                    f"Service type '{self.service_type}' not found. "
                    f"Ensure the service module is imported and has @service('{self.service_type}') decorator."
                )
                return False

            # Get config class from decorator registry
            self._config_class = get_config_class(self.service_type)
            if self._config_class is not None:
                self.logger.debug(
                    f"Config class discovered: {self._config_class.__name__}"
                )
            else:
                # Use base config class if no custom config
                self._config_class = BaseServiceConfig
                self.logger.debug("No config class found, using BaseServiceConfig")

            return True

        except Exception as e:
            self.logger.error(f"Class discovery failed: {e}")
            return False

    async def _setup_configuration(self) -> None:
        """Setup configuration management.

        Uses ProcessContext's config_manager which is already initialized.
        Then applies env var overrides using SERVICE_TYPE_FIELD convention.
        """
        try:
            # Use ProcessContext's config manager
            if not self.process.config_manager:
                raise RuntimeError(
                    "ProcessContext.config_manager not initialized. "
                    "Call ProcessContext.initialize() first."
                )

            self._config_manager = self.process.config_manager

            # Resolve configuration for this service from file/args sources
            config_dict = self._config_manager.resolve_config(
                self.service_type, self.variant
            )

            # Apply env var overrides (SERVICE_TYPE_FIELD convention)
            # Note: For namespaced types like 'halina.server', use underscore: HALINA_SERVER_FIELD
            env_prefix = self.service_type.replace(".", "_")
            env_source = EnvConfigSource(env_prefix)
            if env_source.is_available():
                env_config = env_source.load()
                # Env vars have lower priority than YAML, so merge env first, then config_dict
                config_dict = {**env_config, **config_dict}
                self.logger.debug(
                    f"Applied env config for {env_prefix}: {list(env_config.keys())}"
                )

            # Create config instance
            if issubclass(self._config_class, BaseServiceConfig):
                # Ensure type and variant are set
                if 'type' not in config_dict:
                    config_dict['type'] = self.service_type
                if 'variant' not in config_dict:
                    config_dict['variant'] = self.variant

                # Filter config_dict to only include fields the config class accepts
                filtered_config = self._filter_config_for_class(config_dict, self._config_class)
                self._config = self._config_class(**filtered_config)
            else:
                # Handle non-BaseServiceConfig classes
                self._config = self._config_class()
                for key, value in config_dict.items():
                    if hasattr(self._config, key):
                        setattr(self._config, key, value)

            self.logger.debug("Configuration setup complete")

        except Exception as e:
            self.logger.error(f"Failed to setup configuration: {e}")
            raise

    async def _create_service(self) -> bool:
        """Create service instance."""
        try:
            # Create service with controller reference
            self._service = self._service_class()

            # Set up service properties
            self._service.controller = self
            self._service.svc_config = self._config
            self._service.svc_logger = logging.getLogger(f"svc|{self.service_id}")

            self.logger.debug("Service instance created")
            return True

        except Exception as e:
            self.logger.error(f"Failed to create service: {e}")
            return False

    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._running

    @property
    def config(self) -> BaseServiceConfig | None:
        """Get service configuration."""
        return self._config

    def _filter_config_for_class(
        self, config_dict: dict[str, Any], config_class: type
    ) -> dict[str, Any]:
        """Filter configuration dictionary to only include fields the config class accepts."""
        import inspect
        from dataclasses import fields, is_dataclass

        # Get the configuration class signature
        if is_dataclass(config_class):
            # For dataclasses, get field names
            valid_fields = {field.name for field in fields(config_class)}
        else:
            # For regular classes, get __init__ parameters
            sig = inspect.signature(config_class.__init__)
            valid_fields = set(sig.parameters.keys()) - {'self'}

        # Filter config_dict to only include valid fields
        filtered_config = {k: v for k, v in config_dict.items() if k in valid_fields}

        # Log what was filtered out for debugging
        filtered_out = set(config_dict.keys()) - set(filtered_config.keys())
        if filtered_out:
            self.logger.debug(
                f"Filtered out config fields not supported by {config_class.__name__}: {filtered_out}"
            )

        return filtered_config
