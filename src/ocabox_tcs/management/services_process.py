"""ServicesProcess singleton for shared resources within a process."""

import logging
import asyncio
from typing import Optional, Dict, Any, TYPE_CHECKING
from serverish.messenger import Messenger

if TYPE_CHECKING:
    from .service_controller import ServiceController


class ServicesProcess:
    """Singleton containing common functionality for all ServiceControllers in process."""
    
    _instance: Optional["ServicesProcess"] = None
    _lock = asyncio.Lock()
    
    def __new__(cls) -> "ServicesProcess":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.logger = logging.getLogger("services_process")
        self._messenger: Optional[Messenger] = None
        self._controllers: Dict[str, "ServiceController"] = {}
        self._config_cache: Dict[str, Any] = {}
        self._initialized = True
        self.logger.info("ServicesProcess singleton initialized")
    
    @property
    def messenger(self) -> Optional[Messenger]:
        """Get shared NATS messenger."""
        return self._messenger
    
    async def initialize_messenger(self, nats_url: str = "nats://nats.oca.lan:4222"):
        """Initialize shared NATS messenger."""
        if self._messenger is not None:
            return
        
        try:
            self._messenger = Messenger()
            await self._messenger.connect(nats_url)
            self.logger.info(f"Connected to NATS at {nats_url}")
        except Exception as e:
            self.logger.error(f"Failed to connect to NATS: {e}")
            raise
    
    async def shutdown_messenger(self):
        """Shutdown NATS messenger."""
        if self._messenger:
            await self._messenger.disconnect()
            self._messenger = None
            self.logger.info("Disconnected from NATS")
    
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


