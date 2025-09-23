"""Base service classes and configuration for the universal service framework."""

import logging
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any, TYPE_CHECKING, Type

if TYPE_CHECKING:
    from .management.service_controller import ServiceController


# Registry for decorated classes
_service_registry: Dict[str, Type["BaseService"]] = {}
_config_registry: Dict[str, Type["BaseServiceConfig"]] = {}


def service(service_type: str = None):
    """Decorator to register a service class.
    
    Args:
        service_type: Optional service type identifier. If not provided,
                     uses the class name converted to snake_case.
    
    Example:
        @service("hello_world")
        class HelloWorldService(BasePermanentService):
            pass
            
        @service()  # Will use "hello_world_service" as type
        class HelloWorldService(BasePermanentService):
            pass
    """
    def decorator(cls: Type["BaseService"]) -> Type["BaseService"]:
        # Determine service type
        if service_type is None:
            # Convert class name to snake_case
            type_name = cls.__name__
            if type_name.endswith('Service'):
                type_name = type_name[:-7]  # Remove 'Service' suffix
            # Convert CamelCase to snake_case
            import re
            type_id = re.sub('([A-Z]+)', r'_\1', type_name).lower().lstrip('_')
        else:
            type_id = service_type
        
        # Register the service
        _service_registry[type_id] = cls
        
        # Store type on the class for reference
        cls._service_type = type_id
        
        return cls
    return decorator


def config(service_type: str = None):
    """Decorator to register a config class.
    
    Args:
        service_type: Service type this config belongs to. If not provided,
                     uses the class name converted to snake_case.
    
    Example:
        @config("hello_world")
        class HelloWorldConfig(BaseServiceConfig):
            pass
            
        @config()  # Will use "hello_world" as type (removes _config suffix)
        class HelloWorldConfig(BaseServiceConfig):
            pass
    """
    def decorator(cls: Type["BaseServiceConfig"]) -> Type["BaseServiceConfig"]:
        # Determine service type
        if service_type is None:
            # Convert class name to snake_case
            type_name = cls.__name__
            if type_name.endswith('Config'):
                type_name = type_name[:-6]  # Remove 'Config' suffix
            # Convert CamelCase to snake_case
            import re
            type_id = re.sub('([A-Z]+)', r'_\1', type_name).lower().lstrip('_')
        else:
            type_id = service_type
        
        # Register the config
        _config_registry[type_id] = cls
        
        # Store type on the class and set type field
        cls._service_type = type_id
        if hasattr(cls, '__dataclass_fields__') and 'type' in cls.__dataclass_fields__:
            # Set default value for type field
            cls.__dataclass_fields__['type'].default = type_id
        
        return cls
    return decorator


def get_service_class(service_type: str) -> Optional[Type["BaseService"]]:
    """Get service class by type from decorator registry."""
    return _service_registry.get(service_type)


def get_config_class(service_type: str) -> Optional[Type["BaseServiceConfig"]]:
    """Get config class by type from decorator registry."""
    return _config_registry.get(service_type)


def list_registered_services() -> Dict[str, Type["BaseService"]]:
    """Get all registered services."""
    return _service_registry.copy()


def list_registered_configs() -> Dict[str, Type["BaseServiceConfig"]]:
    """Get all registered configs."""
    return _config_registry.copy()


@dataclass
class BaseServiceConfig:
    """Base configuration for all services."""
    type: str = ""  # Service type identifier
    instance_context: str = ""
    log_level: str = "INFO"

    @property
    def id(self) -> str:
        return f'{self.type}:{self.instance_context}'


class BaseService(ABC):
    """Base class for all services in the universal framework."""

    def __init__(self):
        # These will be set by ServiceController
        self.controller: Optional["ServiceController"] = None
        self.config: Any = None  # Use Any to avoid linter warnings with subclass-specific configs
        self.logger: Optional[logging.Logger] = None
        self._is_running = False
    
    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._is_running

    @property
    def monitor(self):
        """Shortcut to controller.monitor for cleaner API."""
        return self.controller.monitor if self.controller else None
    
    async def _internal_start(self):
        """Internal start method called by ServiceController."""
        self._is_running = True
        await self.start_service()
    
    async def _internal_stop(self):
        """Internal stop method called by ServiceController."""
        await self.stop_service()
        self._is_running = False
    
    @abstractmethod
    async def start_service(self):
        """Service-specific startup logic - override in subclasses."""
        pass
    
    @abstractmethod
    async def stop_service(self):
        """Service-specific cleanup logic - override in subclasses."""
        pass
    
    @classmethod
    def main(cls):
        """Entry point for running service as a script.

        Usage: python service_file.py config.yaml service_type instance_id
        """
        cli_main()


class BasePermanentService(BaseService):
    """Base class for permanent (continuously running) services."""
    
    async def start_service(self):
        """Default implementation for permanent services - override for custom logic."""
        self.logger.info("Permanent service started")
    
    async def stop_service(self):
        """Default implementation for permanent services - override for custom cleanup."""
        self.logger.info("Permanent service stopping")


class BaseBlockingPermanentService(BasePermanentService):
    """Base class for permanent services that block in run_service().
    
    This class handles the common pattern of permanent services that:
    1. Start up (start_service)
    2. Run continuously in a blocking method (run_service) 
    3. Clean up when stopped (stop_service)
    
    The framework automatically manages the task lifecycle.
    """
    
    def __init__(self):
        super().__init__()
        self._main_task: Optional[asyncio.Task] = None
    
    async def start_service(self):
        """Start the service and launch the main task."""
        self.logger.info("Starting blocking permanent service")
        await self.on_start()
        
        # Start the main blocking task
        self._main_task = asyncio.create_task(self._run_wrapper())
        self.logger.info("Blocking permanent service started")
    
    async def stop_service(self):
        """Stop the service and cancel the main task."""
        self.logger.info("Stopping blocking permanent service")
        
        # Cancel the main task
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            self._main_task = None
        
        await self.on_stop()
        self.logger.info("Blocking permanent service stopped")
    
    async def _run_wrapper(self):
        """Wrapper that handles the blocking run_service method."""
        try:
            await self.run_service()
        except asyncio.CancelledError:
            # Expected when service is stopped
            raise
        except Exception as e:
            self.logger.error(f"Error in run_service: {e}")
            raise
    
    async def on_start(self):
        """Called before run_service starts - override for setup logic."""
        pass
    
    async def on_stop(self):
        """Called after run_service stops - override for cleanup logic."""
        pass
    
    @abstractmethod
    async def run_service(self):
        """Main service logic that runs continuously - override in subclasses.
        
        This method should contain the main service loop. It will be called
        in a separate task and should run until the service is stopped.
        
        Example:
            async def run_service(self):
                while self.is_running:
                    # Do work
                    await asyncio.sleep(1)
        """
        pass


class BaseSingleShotService(BaseService):
    """Base class for single-shot/one-time services."""
    
    async def start_service(self):
        """Default implementation - execute and finish."""
        await self.execute()
        # Single-shot services typically stop after execution
    
    async def stop_service(self):
        """Default implementation for single-shot services."""
        self.logger.info("Single-shot service stopped")
    
    @abstractmethod
    async def execute(self):
        """Execute the single-shot task - override in subclasses."""
        pass


# CLI main function for manual service execution
def cli_main():
    """Legacy main entry point for manual service execution."""
    import argparse
    from .management import ServiceController
    
    parser = argparse.ArgumentParser(description="Start an OCM automation service.")
    parser.add_argument("config_file", type=str, help="Path to the config file")
    parser.add_argument("service_type", type=str, help="Type of the service - module name")
    parser.add_argument("service_id", type=str, help="Service instance context/ID")
    args = parser.parse_args()
    
    async def run_service():
        # Create controller
        module_name = f"ocabox_tcs.services.{args.service_type}"
        controller = ServiceController(
            module_name=module_name,
            instance_id=args.service_id
        )
        
        # Initialize and start
        if await controller.initialize(config_file=args.config_file):
            if await controller.start_service():
                try:
                    # Keep running until interrupted
                    while controller.is_running:
                        await asyncio.sleep(1)
                except KeyboardInterrupt:
                    await controller.stop_service()
        
        await controller.shutdown()
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    
    # Run service
    try:
        asyncio.run(run_service())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli_main()