"""ServicesProcess singleton for shared resources within a process."""
from __future__ import annotations
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, TYPE_CHECKING, Union

from ocabox_tcs.management.configuration import ConfigurationManager, create_configuration_manager

if TYPE_CHECKING:
    from .service_controller import ServiceController
    from serverish.messenger import Messenger


class ServicesProcess:
    """Singleton containing common functionality for all ServiceControllers in process."""
    
    _instance: Optional[ServicesProcess] = None
    _lock = asyncio.Lock()
    
    def __new__(cls) -> ServicesProcess:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.logger = logging.getLogger("svcs_process")
        self._messenger: Optional[Messenger] = None
        self._controllers: Dict[str, ServiceController] = {}
        self.config_manager: Optional[ConfigurationManager] = None
        self._config_cache: Dict[str, Any] = {}
        self._initialized = True
        self.logger.info("ServicesProcess singleton initialized")
    
    @property
    def messenger(self) -> Optional[Messenger]:
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
                    self.logger.warning(f"Messenger already open")
                    return
                else:
                    self.logger.warning(f"Messenger already open, reopening")
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
    
    def register_controller(self, controller: "ServiceController"):
        """Register a service controller."""
        service_id = f"{controller.module_name}:{controller.instance_id}"
        self._controllers[service_id] = controller
        self.logger.info(f"Registered controller: {service_id}")
    
    def unregister_controller(self, controller: "ServiceController"):
        """Unregister a service controller."""
        service_id = f"{controller.module_name}:{controller.instance_id}"
        if service_id in self._controllers:
            del self._controllers[service_id]
            self.logger.info(f"Unregistered controller: {service_id}")
    
    def get_controller(self, module_name: str, instance_id: str) -> Optional["ServiceController"]:
        """Get a registered controller."""
        service_id = f"{module_name}:{instance_id}"
        return self._controllers.get(service_id)
    
    def cache_config(self, key: str, config: Any):
        """Cache configuration data."""
        self._config_cache[key] = config
        self.logger.debug(f"Cached config for: {key}")
    
    def get_cached_config(self, key: str) -> Optional[Any]:
        """Get cached configuration data."""
        return self._config_cache.get(key)
    
    def clear_config_cache(self):
        """Clear configuration cache."""
        self._config_cache.clear()
        self.logger.debug("Cleared config cache")
    
    async def shutdown(self):
        """Shutdown the process and all controllers."""
        self.logger.info("Shutting down ServicesProcess")
        
        # Shutdown all controllers
        for controller in list(self._controllers.values()):
            try:
                await controller.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down controller {controller}: {e}")
        
        # Shutdown messenger
        await self.shutdown_messenger()
        
        # Clear singleton
        ServicesProcess._instance = None
        self.logger.info("ServicesProcess shutdown complete")

    async def get_configuration_manager(self,
                                        config_file: Optional[Union[str, Path]] = None,
                                        args_config: Optional[Dict[str, Any]] = None,
                                        config_subject: Optional[str] = None,
                                        config_messenger: Any = None,
                                        defaults: Optional[Dict[str, Any]] = None
                                        ) -> ConfigurationManager:
        """Creates if not exists and returns a process-wide ConfigurationManager."""
        async with self._lock:
            if self.config_manager is None:
                self.config_manager = await create_configuration_manager(
                    config_file=config_file,
                    args_config=args_config,
                    config_subject=config_subject,
                    config_messenger=config_messenger or self._messenger,
                    defaults=defaults
                )
                self.logger.info("ConfigurationManager created")
            return self.config_manager

