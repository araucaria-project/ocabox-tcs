"""ServiceController for managing individual service instances."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    pass

from ocabox_tcs.management.configuration import ConfigurationManager
from ocabox_tcs.management.process_context import ProcessContext

from ..base_service import BaseService, BaseServiceConfig
from ..monitoring import MessengerMonitoredObject, Status


class ServiceController:
    """Controls single service in same process as service."""
    
    def __init__(self, module_name: str, instance_id: str,
                 config_sources: dict[str, Any] | None = None,
                 runner_id: str | None = None):
        self.module_name = module_name
        self.instance_id = instance_id
        self.runner_id = runner_id
        self.service_id = f"{module_name}:{instance_id}"

        self.logger = logging.getLogger(f"ctrl.{self._short_service_id()}")
        self.process = ProcessContext()
        
        # Initialize monitoring
        self.monitor: MessengerMonitoredObject | None = None
        
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
        
        self.logger.info(f"Created controller for {self.service_id}")
    
    async def initialize(self) -> bool:
        """Initialize the controller and discover service classes.

        Note: ProcessContext must already be initialized before calling this.
        """
        if self._initialized:
            return True

        try:
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
            self.logger.info("Controller initialized successfully")
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
            # if not await self.initialize():
            #     return False
        
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
        
        # Stop monitoring
        if self.monitor:
            if hasattr(self.monitor, 'send_shutdown'):
                await self.monitor.send_shutdown()
            await self.monitor.stop_monitoring()
        
        # Unregister from process
        self.process.unregister_controller(self)
        
        self.logger.info("Controller shutdown complete")
    
    async def _initialize_monitoring(self):
        """Initialize monitoring system."""
        try:
            # Try to ensure messenger is available
            if not self.process.messenger:
                self.logger.warning("No NATS messenger configured, cannot initialize monitoring publication")

            # Create monitor (check_interval is heartbeat interval, default 10s)
            self.monitor = MessengerMonitoredObject(
                name=self.service_id,
                messenger=self.process.messenger,
                check_interval=10.0
            )

            # Pass runner_id to monitor so it can include it in registry messages
            if self.runner_id:
                self.monitor.runner_id = self.runner_id

            # Set initial status BEFORE starting monitoring (to avoid initial "unknown" report)
            self.monitor.set_status(Status.STARTUP, "Initializing monitoring")

            # Start monitoring
            await self.monitor.start_monitoring()
            await self.monitor.send_registration()

            self.logger.debug("Monitoring initialized with NATS")
        except Exception as e:
            # Fall back to basic monitoring without NATS
            self.logger.warning(f"Failed to initialize NATS monitoring, using local monitoring: {e}")
            from ..monitoring import ReportingMonitoredObject
            self.monitor = ReportingMonitoredObject(
                name=self.service_id,
                check_interval=10.0
            )
            # Set initial status BEFORE starting monitoring
            self.monitor.set_status(Status.STARTUP, "Initializing monitoring")
            await self.monitor.start_monitoring()
            self.logger.debug("Monitoring initialized without NATS")
    
    async def _discover_classes(self) -> bool:
        """Discover service and config classes."""
        try:
            # Import the module to trigger decorator registration
            module = importlib.import_module(self.module_name)

            # Extract service type from module name
            # For ocabox_tcs.services.examples.01_minimal → try both:
            #   1. examples.01_minimal (subdirectory path)
            #   2. 01_minimal (just filename)
            module_parts = self.module_name.split('.')
            service_type_full = module_parts[-1]  # Just filename

            # Try to get relative path from 'services'
            if 'services' in module_parts:
                services_idx = module_parts.index('services')
                service_type_with_path = '.'.join(module_parts[services_idx + 1:])
            else:
                service_type_with_path = service_type_full

            # 1. Try decorator registry first (try path-aware type first, then fallback)
            from ..base_service import get_config_class, get_service_class

            self._service_class = get_service_class(service_type_with_path)
            if self._service_class is None:
                self._service_class = get_service_class(service_type_full)

            if self._service_class is not None:
                self.logger.debug(f"Service class discovered via @service decorator: {self._service_class.__name__}")

            self._config_class = get_config_class(service_type_with_path)
            if self._config_class is None:
                self._config_class = get_config_class(service_type_full)

            if self._config_class is not None:
                self.logger.debug(f"Config class discovered via @config decorator: {self._config_class.__name__}")
            
            # 2. Fall back to module variable approach (deprecated)
            if self._service_class is None and hasattr(module, 'service_class'):
                self._service_class = module.service_class
                self.logger.warning(
                    f"Service class discovered via deprecated 'service_class' module variable in {self.module_name}. "
                    f"Please migrate to decorator-based discovery by adding '@service' decorator to your service class. "
                    f"Example: @service\\nclass {self._service_class.__name__}(BaseService): ..."
                )

            if self._config_class is None and hasattr(module, 'config_class'):
                self._config_class = module.config_class
                self.logger.warning(
                    f"Config class discovered via deprecated 'config_class' module variable in {self.module_name}. "
                    f"Please migrate to decorator-based discovery by adding '@config' decorator to your config class. "
                    f"Example: @config\\n@dataclass\\nclass {self._config_class.__name__}(BaseServiceConfig): ..."
                )

            # 3. Try convention-based discovery for service (undocumented heuristic)
            if self._service_class is None:
                class_name = ''.join(word.capitalize() for word in service_type_full.split('_')) + 'Service'
                if hasattr(module, class_name):
                    self._service_class = getattr(module, class_name)
                    self.logger.warning(
                        f"Service class discovered via naming convention heuristic in {self.module_name}. "
                        f"This is an undocumented fallback and may fail in future versions. "
                        f"Please add '@service' decorator to your service class for reliable discovery. "
                        f"Example: @service\\nclass {class_name}(BaseService): ..."
                    )

            # 4. Try convention-based discovery for config (undocumented heuristic)
            if self._config_class is None and self._service_class is not None:
                class_name = self._service_class.__name__.replace('Service', 'Config')
                if hasattr(module, class_name):
                    self._config_class = getattr(module, class_name)
                    self.logger.warning(
                        f"Config class discovered via naming convention heuristic in {self.module_name}. "
                        f"This is an undocumented fallback and may fail in future versions. "
                        f"Please add '@config' decorator to your config class for reliable discovery. "
                        f"Example: @config\\n@dataclass\\nclass {class_name}(BaseServiceConfig): ..."
                    )
            
            # 5. Service class is required
            if self._service_class is None:
                self.logger.error(f"Could not find service class in {self.module_name}")
                return False
            
            # 6. Config class is optional - use base class if not found
            if self._config_class is None:
                from ..base_service import BaseServiceConfig
                self._config_class = BaseServiceConfig
                self.logger.debug("No config class found, using BaseServiceConfig")
            
            self.logger.debug(f"Discovered classes: {self._service_class.__name__}, {self._config_class.__name__}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to discover classes: {e}")
            return False
    
    async def _setup_configuration(self) -> None:
        """Setup configuration management.

        Uses ProcessContext's config_manager which is already initialized.
        """
        try:
            # Use ProcessContext's config manager
            if not self.process.config_manager:
                raise RuntimeError("ProcessContext.config_manager not initialized. Call ProcessContext.initialize() first.")

            self._config_manager = self.process.config_manager

            # Resolve configuration for this service
            config_dict = self._config_manager.resolve_config(
                self.module_name, self.instance_id
            )

            # Extract service type for config
            module_parts = self.module_name.split('.')
            service_type = module_parts[-1]

            # Create config instance
            if issubclass(self._config_class, BaseServiceConfig):
                # Ensure type and instance_context are set
                if 'type' not in config_dict:
                    config_dict['type'] = service_type
                if 'instance_context' not in config_dict:
                    config_dict['instance_context'] = self.instance_id

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
            self._service.config = self._config
            self._service.logger = logging.getLogger(f"svc.{self._short_service_id()}")
            
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
    
    def _filter_config_for_class(self, config_dict: dict[str, Any], config_class: type) -> dict[str, Any]:
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
            self.logger.debug(f"Filtered out config fields not supported by {config_class.__name__}: {filtered_out}")
        
        return filtered_config

    def _short_service_id(self) -> str:
        """Get short service ID for compact logging.

        Converts: ocabox_tcs.services.hello_world:dev → hello_world:dev
        """
        service_type = self.module_name.split('.')[-1]
        return f"{service_type}:{self.instance_id}"