"""Base service classes and configuration for the universal service framework."""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .monitoring import Status

if TYPE_CHECKING:
    from .management.service_controller import ServiceController


# Registry for decorated classes
_service_registry: dict[str, type["BaseService"]] = {}
_config_registry: dict[str, type["BaseServiceConfig"]] = {}


def _class_name_to_type(class_name: str) -> str:
    """Convert class name to service type (fallback only)."""
    import re
    type_name = class_name
    if type_name.endswith('Service'):
        type_name = type_name[:-7]  # Remove 'Service' suffix
    # Convert CamelCase to snake_case
    return re.sub('([A-Z]+)', r'_\1', type_name).lower().lstrip('_')


def service(cls: type["BaseService"]) -> type["BaseService"]:
    """Decorator to register a service class.

    Service type is automatically derived from the filename where the class is defined.
    The filename (without .py extension) must match the service type used in config files.

    Example:
        # File: hello_world.py
        @service
        class HelloWorldService(BasePermanentService):
            pass
        # → Registers as service type "hello_world"

        # File: guider_service.py
        @service
        class GuiderService(BasePermanentService):
            pass
        # → Registers as service type "guider_service"
    """
    try:
        # Get the file where the class is defined
        import inspect
        import os
        from pathlib import Path

        filename = inspect.getfile(cls)
        file_path = Path(filename).resolve()

        # Handle __main__ case (when run as script)
        if os.path.splitext(os.path.basename(filename))[0] == '__main__':
            import sys
            script_path = sys.argv[0] if sys.argv else filename
            file_path = Path(script_path).resolve()

        # Try to detect if service is in a subdirectory under services/
        # e.g., services/examples/01_minimal.py → examples.01_minimal
        try:
            # Find 'services' directory in path
            parts = file_path.parts
            if 'services' in parts:
                services_idx = len(parts) - list(reversed(parts)).index('services') - 1
                # Get relative path from services/ directory
                rel_parts = parts[services_idx + 1:]
                # Remove .py extension from last part
                rel_parts = list(rel_parts[:-1]) + [Path(rel_parts[-1]).stem]
                # Join with dots
                type_id = '.'.join(rel_parts)
            else:
                # Fallback to just filename
                type_id = file_path.stem
        except (ValueError, IndexError):
            # Fallback to just filename
            type_id = file_path.stem

        # Derive module name from file path (for Feature #7: external module support)
        # This allows services outside ocabox_tcs.services to work correctly
        module_name = None
        try:
            # Try to construct module path from file path
            # Look for common package markers (src/, tests/, etc.)
            parts = file_path.parts

            # Find a package root marker (src/, tests/)
            for i, part in enumerate(parts):
                if part in ('src', 'tests'):
                    # Build module path from this marker (inclusive) onward
                    # For src/, skip it (src/ocabox_tcs/... → ocabox_tcs....)
                    # For tests/, include it (tests/services/... → tests.services....)
                    if part == 'src':
                        module_parts = list(parts[i+1:])
                    else:  # tests or other markers
                        module_parts = list(parts[i:])
                    # Remove .py extension
                    module_parts[-1] = Path(module_parts[-1]).stem
                    module_name = '.'.join(module_parts)
                    break

            # If no marker found and cls.__module__ is not __main__, use it
            if module_name is None and cls.__module__ != '__main__':
                module_name = cls.__module__
        except Exception:
            pass

    except Exception as e:
        # Fallback to class name conversion
        import logging
        type_id = _class_name_to_type(cls.__name__)
        module_name = None
        logger = logging.getLogger("service.decorator")
        logger.warning(
            f"Could not derive service type from filename for {cls.__name__}: {e}. "
            f"Using class-name-derived type '{type_id}' instead. "
            f"This may cause service discovery issues - ensure filename matches expected service type."
        )

    # Register the service
    _service_registry[type_id] = cls

    # Store type and module name on the class for reference
    cls._service_type = type_id
    if module_name:
        cls._module_name = module_name

    return cls


def config(cls: type["BaseServiceConfig"]) -> type["BaseServiceConfig"]:
    """Decorator to register a config class.

    Service type is automatically derived from the filename where the class is defined.
    The filename (without .py extension) must match the service type used in config files.

    Example:
        # File: hello_world.py
        @config
        class HelloWorldConfig(BaseServiceConfig):
            pass
        # → Registers as config for service type "hello_world"

        # File: guider_service.py
        @config
        class GuiderConfig(BaseServiceConfig):
            pass
        # → Registers as config for service type "guider_service"
    """
    try:
        # Get the file where the class is defined
        import inspect
        import os
        from pathlib import Path

        filename = inspect.getfile(cls)
        file_path = Path(filename).resolve()

        # Handle __main__ case (when run as script)
        if os.path.splitext(os.path.basename(filename))[0] == '__main__':
            import sys
            script_path = sys.argv[0] if sys.argv else filename
            file_path = Path(script_path).resolve()

        # Try to detect if service is in a subdirectory under services/
        # e.g., services/examples/02_basic.py → examples.02_basic
        try:
            # Find 'services' directory in path
            parts = file_path.parts
            if 'services' in parts:
                services_idx = len(parts) - list(reversed(parts)).index('services') - 1
                # Get relative path from services/ directory
                rel_parts = parts[services_idx + 1:]
                # Remove .py extension from last part
                rel_parts = list(rel_parts[:-1]) + [Path(rel_parts[-1]).stem]
                # Join with dots
                type_id = '.'.join(rel_parts)
            else:
                # Fallback to just filename
                type_id = file_path.stem
        except (ValueError, IndexError):
            # Fallback to just filename
            type_id = file_path.stem
    except Exception as e:
        # Fallback to class name conversion (remove 'Config' suffix)
        import logging
        type_name = cls.__name__
        if type_name.endswith('Config'):
            type_name = type_name[:-6]
        type_id = _class_name_to_type(type_name)
        logger = logging.getLogger("config.decorator")
        logger.warning(
            f"Could not derive config type from filename for {cls.__name__}: {e}. "
            f"Using class-name-derived type '{type_id}' instead."
        )

    # Register the config
    _config_registry[type_id] = cls

    # Store type on the class and set type field
    cls._service_type = type_id
    if hasattr(cls, '__dataclass_fields__') and 'type' in cls.__dataclass_fields__:
        # Set default value for type field
        cls.__dataclass_fields__['type'].default = type_id

    return cls


def get_service_class(service_type: str) -> type["BaseService"] | None:
    """Get service class by type from decorator registry."""
    return _service_registry.get(service_type)


def get_config_class(service_type: str) -> type["BaseServiceConfig"] | None:
    """Get config class by type from decorator registry."""
    return _config_registry.get(service_type)


def list_registered_services() -> dict[str, type["BaseService"]]:
    """Get all registered services."""
    return _service_registry.copy()


def list_registered_configs() -> dict[str, type["BaseServiceConfig"]]:
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
    _service_type: str = None  # To be set by decorator

    def __init__(self):
        # These will be set by ServiceController
        self.controller: ServiceController | None = None
        self.config: Any = None  # Use Any to avoid linter warnings with subclass-specific configs
        self.logger: logging.Logger | None = None
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

        Usage: python service_file.py config.yaml instance_context [--runner-id ID]

        The service type is automatically derived from the filename.

        This is a thin wrapper that:
        1. Initializes ProcessContext (once per process)
        2. Creates and runs ServiceController
        3. Waits for shutdown
        """
        import argparse
        import signal

        from ocabox_tcs.management.process_context import ProcessContext
        from ocabox_tcs.management.service_controller import ServiceController

        parser = argparse.ArgumentParser(description="Start a TCS service.")
        parser.add_argument("config_file", type=str, help="Path to the config file")
        parser.add_argument("instance_context", type=str, help="Service instance context/ID")
        parser.add_argument("--runner-id", type=str, help="Optional runner ID for monitoring")
        parser.add_argument("--no-banner", action="store_true", help="Suppress startup banner")
        args = parser.parse_args()

        service_type = cls._service_type

        # Setup logging first - basic format for subprocess output
        logging.basicConfig(
            level=logging.INFO,
            format='[%(levelname)-5s] %(name)-12s: %(message)s'
        )

        # Print startup banner (unless suppressed)
        if not args.no_banner:
            logger = logging.getLogger("launch")
            logger.info("=" * 60)
            logger.info("TCS - Telescope Control Services")
            logger.info(f"Standalone Service: {service_type}:{args.instance_context}")
            logger.info("=" * 60)

        async def amain():
            """Async main function for service."""
            # Initialize ProcessContext (once per process)
            process_ctx = await ProcessContext.initialize(config_file=args.config_file)

            # Create controller
            # Support external modules (Feature #7) - use decorator's captured module name
            if hasattr(cls, '_module_name'):
                module_name = cls._module_name
            else:
                # Fallback for legacy services without decorator info
                module_name = f"ocabox_tcs.services.{service_type}"

            controller = ServiceController(
                module_name=module_name,
                instance_id=args.instance_context,
                runner_id=args.runner_id
            )

            # Initialize and start service
            if await controller.initialize():
                if await controller.start_service():
                    try:
                        # Wait for shutdown signal
                        shutdown_event = asyncio.Event()
                        loop = asyncio.get_running_loop()

                        def handle_signal():
                            shutdown_event.set()

                        # Set up signal handlers (asyncio-aware)
                        loop.add_signal_handler(signal.SIGINT, handle_signal)
                        loop.add_signal_handler(signal.SIGTERM, handle_signal)

                        # Wait for shutdown
                        await shutdown_event.wait()
                        await controller.stop_service()
                    except KeyboardInterrupt:
                        await controller.stop_service()

            await controller.shutdown()
            await process_ctx.shutdown()

        # Run service
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            pass


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
    1. Start up (on_start hook)
    2. Run continuously in a blocking method (run_service)
    3. Clean up when stopped (on_stop hook)

    The framework automatically manages the task lifecycle.

    IMPORTANT: Do NOT override start_service() or stop_service() in subclasses!
    - Override run_service() for your main service loop
    - Override on_start() for initialization (optional)
    - Override on_stop() for cleanup (optional)

    The base class's start_service()/stop_service() manage the task lifecycle.
    """

    def __init_subclass__(cls, **kwargs):
        """Validate that subclasses don't override protected methods."""
        super().__init_subclass__(**kwargs)

        # Check if this class directly overrides start_service
        if 'start_service' in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} should not override start_service(). "
                f"For BaseBlockingPermanentService subclasses, override run_service() instead, "
                f"and optionally use on_start()/on_stop() hooks for setup/cleanup. "
                f"See BaseBlockingPermanentService docstring for details."
            )

        # Check if this class directly overrides stop_service
        if 'stop_service' in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} should not override stop_service(). "
                f"For BaseBlockingPermanentService subclasses, override run_service() instead, "
                f"and optionally use on_start()/on_stop() hooks for setup/cleanup. "
                f"See BaseBlockingPermanentService docstring for details."
            )

    def __init__(self):
        super().__init__()
        self._main_task: asyncio.Task | None = None

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
            self.monitor.set_status(Status.ERROR, f"Error in run_service: {e}")
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

