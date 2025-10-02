"""ProcessContext singleton for shared resources within a process."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from serverish.messenger import Messenger

from ocabox_tcs.management.configuration import (
    ArgsConfigSource,
    ConfigurationManager,
    FileConfigSource,
    NATSConfigSource,
)


if TYPE_CHECKING:
    from ocabox_tcs.management.service_controller import ServiceController


class ProcessContext:
    """Singleton context for all services in a process.

    Manages:
    - Configuration (file → NATS bootstrap)
    - NATS Messenger (singleton, shared)
    - Service registry (controllers in this process)
    """

    _instance: ProcessContext | None = None
    _lock = asyncio.Lock()

    def __new__(cls) -> ProcessContext:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.logger = logging.getLogger("ctx")
        self._messenger: Messenger | None = None
        self._controllers: dict[str, ServiceController] = {}
        self.config_manager: ConfigurationManager | None = None
        self.config_file: str | None = None  # Store original config file path
        self._config_cache: dict[str, Any] = {}
        self._initialized = True
        self.logger.info("ProcessContext singleton initialized")
    
    @property
    def messenger(self) -> Messenger | None:
        """Get shared NATS messenger."""
        return self._messenger
    
    async def initialize_messenger(self, host: str | None = None, port: int | None = None,
                   wait: float | bool = True, timeout: float | None = None, force_reopen: bool = False):
        """Initialize shared NATS messenger."""
        if self._messenger is not None:
            return
        
        try:
            self._messenger = Messenger()
            if self._messenger.is_open:
                if not force_reopen:
                    self.logger.warning("Messenger already open")
                    return
                else:
                    self.logger.warning("Messenger already open, reopening")
                    await self._messenger.close()


            await self._messenger.open(host=host, port=port, wait=wait, timeout=timeout)
            self.logger.info(f"Messenger opened, connected to {host}:{port}")
        except Exception as e:
            self.logger.error(f"Failed to open messenger to {host}:{port}: {e}")
            self._messenger = None
            raise
    
    async def shutdown_messenger(self):
        """Shutdown NATS messenger.

        Note: Only closes messenger if this process owns it. Since Messenger is a singleton,
        closing it affects all users in the process.
        """
        if self._messenger:
            if self._messenger.is_open:
                await self._messenger.close()
                self.logger.info("Closed NATS messenger")
            self._messenger = None
    
    def register_controller(self, controller: ServiceController):
        """Register a service controller."""
        service_id = f"{controller.module_name}:{controller.instance_id}"
        self._controllers[service_id] = controller
        self.logger.info(f"Registered controller: {service_id}")
    
    def unregister_controller(self, controller: ServiceController):
        """Unregister a service controller."""
        service_id = f"{controller.module_name}:{controller.instance_id}"
        if service_id in self._controllers:
            del self._controllers[service_id]
            self.logger.info(f"Unregistered controller: {service_id}")
    
    def get_controller(self, module_name: str, instance_id: str) -> ServiceController | None:
        """Get a registered controller."""
        service_id = f"{module_name}:{instance_id}"
        return self._controllers.get(service_id)
    
    def cache_config(self, key: str, config: Any):
        """Cache configuration data."""
        self._config_cache[key] = config
        self.logger.debug(f"Cached config for: {key}")
    
    def get_cached_config(self, key: str) -> Any | None:
        """Get cached configuration data."""
        return self._config_cache.get(key)
    
    def clear_config_cache(self):
        """Clear configuration cache."""
        self._config_cache.clear()
        self.logger.debug("Cleared config cache")
    
    @classmethod
    async def initialize(cls, config_file: str, args_config: dict[str, Any] | None = None) -> ProcessContext:
        """Initialize process-wide resources. Call once per OS process.

        Used by:
        - Standalone service processes
        - Asyncio launcher process (shared with all services)
        - Process launcher process (for launcher itself)
        - Each subprocess spawned by process launcher

        Initialization steps:
        1. Configuration manager (file + args)
        2. NATS messenger (if configured)
        3. NATS config source (if configured)

        Args:
            config_file: Path to configuration file
            args_config: Optional configuration from command line arguments

        Returns:
            ProcessContext singleton instance
        """
        instance = cls()  # Get singleton

        # Store config file path for later use (e.g., passing to spawned processes)
        instance.config_file = config_file

        # Always initialize config manager
        await instance._init_config_manager(config_file, args_config)

        # NATS initialization (optional, based on config)
        global_config = instance.config_manager.resolve_config()
        nats_config = global_config.get("nats", {})

        if nats_config:
            await instance._init_messenger(nats_config)
            await instance._add_nats_config_source(nats_config)

        instance.logger.info("ProcessContext initialized")
        return instance

    async def _init_config_manager(self, config_file: str, args_config: dict[str, Any] | None = None):
        """Initialize configuration manager from file and args."""
        self.config_manager = ConfigurationManager()

        if config_file:
            self.config_manager.add_source(FileConfigSource(config_file))
            self.logger.debug(f"Added file config source: {config_file}")

        if args_config:
            self.config_manager.add_source(ArgsConfigSource(args_config))
            self.logger.debug("Added args config source")

        self.logger.debug("Configuration manager initialized")

    async def _init_messenger(self, nats_config: dict[str, Any]):
        """Initialize NATS messenger from config.

        If 'required' is True in config, will block until NATS is available.
        Otherwise uses a short timeout to avoid blocking startup.
        """
        host = nats_config.get("host", "localhost")
        port = nats_config.get("port", 4222)
        required = nats_config.get("required", True)  # Default: block until NATS available

        # If NATS is required, block until connected (useful for server restart scenarios)
        # Otherwise use short timeout to avoid blocking startup if NATS is unavailable
        timeout = None if required else 2.0

        try:
            await self.initialize_messenger(host=host, port=port, timeout=timeout)
            self.logger.debug(f"NATS messenger initialized: {host}:{port}")
        except Exception as e:
            if required:
                # If required, re-raise the exception to fail startup
                self.logger.error(f"NATS is required but connection failed: {e}")
                raise
            else:
                # If optional, log warning and continue without NATS
                self.logger.warning(f"Failed to initialize NATS messenger: {e}")
                # Continue without NATS - not critical

    async def _add_nats_config_source(self, nats_config: dict[str, Any]):
        """Add NATS as a configuration source if configured."""
        config_subject = nats_config.get("config_subject")

        if config_subject and self.messenger:
            self.config_manager.add_source(
                NATSConfigSource(config_subject, self.messenger)
            )
            self.logger.debug(f"NATS config source added: {config_subject}")

    async def shutdown(self):
        """Shutdown the process and all controllers."""
        self.logger.info("Shutting down ProcessContext")

        # Shutdown all controllers
        for controller in list(self._controllers.values()):
            try:
                await controller.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down controller {controller}: {e}")

        # Shutdown messenger
        await self.shutdown_messenger()

        # Clear singleton
        ProcessContext._instance = None
        self.logger.info("ProcessContext shutdown complete")

