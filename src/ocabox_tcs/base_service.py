"""Base service classes and configuration for the universal service framework."""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .monitoring import Status

if TYPE_CHECKING:
    from .management.service_controller import ServiceController


_log = logging.getLogger("svc.base")

# Registry for decorated classes
_service_registry: dict[str, type["BaseService"]] = {}
_config_registry: dict[str, type["BaseServiceConfig"]] = {}


def service(service_type: str):
    """Decorator to register a service class.

    The service_type parameter is REQUIRED and must match the type used in
    services.yaml configuration files and the registry section.

    Args:
        service_type: Explicit service type identifier. Can contain dots for
                     namespacing (e.g., 'examples.minimal', 'halina.server').

    Example:
        @service('hello_world')
        class HelloWorldService(BasePermanentService):
            pass

        @service('examples.minimal')  # Dots allowed for grouping
        class MinimalService(BaseBlockingPermanentService):
            pass

    The service_type is used for:
    - Registry lookup in services.yaml
    - NATS subject construction (svc.status.{type}.{variant})
    - tcsctl display
    - Service identification throughout the system
    """
    if not isinstance(service_type, str):
        raise TypeError(
            f"@service decorator requires a string service_type argument. "
            f"Usage: @service('my_service_type'). Got: {type(service_type).__name__}"
        )

    if not service_type:
        raise ValueError(
            "@service decorator requires a non-empty service_type argument. "
            "Usage: @service('my_service_type')"
        )

    def decorator(cls: type["BaseService"]) -> type["BaseService"]:
        # Register the service in global registry
        _service_registry[service_type] = cls

        # Store type on the class for reference
        cls._service_type = service_type

        # Module name is just for reference - use what Python provides
        # No path parsing magic needed since we have explicit service_type
        cls._module_name = cls.__module__

        _log.debug(f"Registered service '{service_type}' -> {cls.__name__}")

        return cls

    return decorator


def config(service_type: str):
    """Decorator to register a config class.

    The service_type parameter is REQUIRED and must match the @service decorator
    type for the corresponding service class.

    Args:
        service_type: Explicit service type identifier. Must match the
                     @service decorator type.

    Example:
        @config('hello_world')
        @dataclass
        class HelloWorldConfig(BaseServiceConfig):
            message: str = "Hello!"
            interval: int = 5

        @config('examples.minimal')
        @dataclass
        class MinimalConfig(BaseServiceConfig):
            pass
    """
    if not isinstance(service_type, str):
        raise TypeError(
            f"@config decorator requires a string service_type argument. "
            f"Usage: @config('my_service_type'). Got: {type(service_type).__name__}"
        )

    if not service_type:
        raise ValueError(
            "@config decorator requires a non-empty service_type argument. "
            "Usage: @config('my_service_type')"
        )

    def decorator(cls: type["BaseServiceConfig"]) -> type["BaseServiceConfig"]:
        # Register the config
        _config_registry[service_type] = cls
        _log.debug(f"Registered config '{service_type}' -> {cls.__name__}")

        # Store type on the class and set type field default
        cls._service_type = service_type
        if hasattr(cls, '__dataclass_fields__') and 'type' in cls.__dataclass_fields__:
            cls.__dataclass_fields__['type'].default = service_type

        return cls

    return decorator


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
    """Base configuration for all services.

    Attributes:
        type: Service type identifier (e.g., 'hello_world', 'halina.server')
        variant: Instance identifier - distinguishes multiple instances of same type.
                 Cannot contain dots. Examples: 'dev', 'prod', 'jk15'
        log_level: Logging level for the service
    """
    type: str = ""
    variant: str = ""
    log_level: str = "INFO"

    @property
    def id(self) -> str:
        """Full service identifier in format: {type}.{variant}"""
        return f'{self.type}.{self.variant}'


class BaseService(ABC):
    """Base class for all services in the universal framework."""
    _service_type: str = None  # To be set by decorator

    def __init__(self):
        # These will be set by ServiceController
        self.controller: ServiceController | None = None
        self.svc_config: Any = None  # Service config - renamed to avoid collision with user code
        self.svc_logger: logging.Logger | None = None  # Service logger - renamed to avoid collision with user code

    @property
    def is_running(self) -> bool:
        """Check if service is running.

        Delegates to controller which owns the running state.
        """
        if not self.controller:
            return False
        return self.controller.is_running

    def is_stopping(self) -> bool:
        """Check if service is being stopped.

        Delegates to controller which owns the stop signal.

        Returns:
            True if stop has been signaled, False otherwise
        """
        if not self.controller:
            return False
        return self.controller.is_stopping()

    async def sleep(self, seconds: float | None = None) -> bool:
        """Exit-aware sleep that wakes immediately when service stops.

        This is the recommended way to sleep in services, as it allows
        immediate wakeup when the service is being stopped.

        Delegates to controller which owns the stop event.

        Args:
            seconds: Time to sleep in seconds, or None to wait indefinitely for stop

        Returns:
            True if sleep completed normally, False if interrupted by stop signal

        Example:
            # Sleep for 5 seconds (or until stop)
            if await self.sleep(5.0):
                # Sleep completed normally
                self.svc_logger.info("Work cycle completed")
            else:
                # Service stopping - exit loop
                self.svc_logger.info("Service stopping, exiting loop")
                return

            # Wait indefinitely for stop signal
            await self.sleep(None)  # Blocks until service stops
        """
        if not self.controller:
            # Fallback if no controller (shouldn't happen in normal operation)
            if seconds is None:
                await asyncio.Event().wait()  # Wait forever
            else:
                await asyncio.sleep(seconds)
            return True
        return await self.controller.sleep(seconds)

    @property
    def monitor(self):
        """Shortcut to controller.monitor for cleaner API."""
        return self.controller.monitor if self.controller else None

    async def _internal_start(self):
        """Internal start method called by ServiceController.

        Controller owns the running state, so we just call start_service().
        """
        await self.start_service()

    async def _internal_stop(self):
        """Internal stop method called by ServiceController.

        Controller owns the running state, so we just call stop_service().
        """
        await self.stop_service()

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

        Usage:
            python service_file.py                    # Use defaults (variant='dev')
            python service_file.py variant            # Custom variant, no config
            python service_file.py config.yaml variant # Full specification

        The service type is taken from the @service decorator.

        This is a thin wrapper that:
        1. Initializes ProcessContext (once per process)
        2. Creates and runs ServiceController
        3. Waits for shutdown
        """
        import argparse
        import signal

        from ocabox_tcs.management.process_context import ProcessContext
        from ocabox_tcs.management.service_controller import ServiceController
        from ocabox_tcs.management.service_registry import ServiceRegistry, validate_variant

        parser = argparse.ArgumentParser(description="Start a TCS service.")
        parser.add_argument(
            "config_file",
            nargs='?',
            default=None,
            type=str,
            help="Path to the config file (optional, defaults to None)"
        )
        parser.add_argument(
            "variant",
            nargs='?',
            default="dev",
            type=str,
            help="Service variant identifier (optional, defaults to 'dev')"
        )
        parser.add_argument("--runner-id", type=str, help="Optional runner ID for monitoring")
        parser.add_argument("--parent-name", type=str, default=None,
                           help="Parent entity name for hierarchical display in tcsctl")
        parser.add_argument("--no-banner", action="store_true", help="Suppress startup banner")
        args = parser.parse_args()

        # Smart detection: if config_file is provided but variant is "dev" (default),
        # check if config_file looks like a file path (ends with .yaml/.yml)
        # If not, treat it as variant instead
        if args.config_file is not None and args.variant == "dev":
            # Single argument provided - is it a config file or variant?
            if not (args.config_file.endswith('.yaml') or args.config_file.endswith('.yml')):
                # Doesn't look like a config file - treat as variant
                args.variant = args.config_file
                args.config_file = None

        # Validate variant has no dots
        try:
            validate_variant(args.variant)
        except ValueError as e:
            import sys
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        # Get service type from decorator
        if not hasattr(cls, '_service_type') or cls._service_type is None:
            raise TypeError(
                f"Service class {cls.__name__} has no @service decorator. "
                f"Use @service('service_type') to register the class."
            )
        service_type = cls._service_type

        # Load .env file if it exists (before logging setup)
        from ocabox_tcs.management.environment import load_dotenv_if_available
        env_loaded, env_file_path = load_dotenv_if_available()

        # Setup logging first - basic format for subprocess output
        logging.basicConfig(
            level=logging.INFO,
            format='[%(levelname)-5s] %(name)-12s: %(message)s'
        )

        # Print startup banner (unless suppressed)
        if not args.no_banner:
            logger = logging.getLogger("launch")
            # Log if .env was loaded
            if env_loaded and env_file_path:
                logger.info(f"Loaded environment from {env_file_path}")
            logger.info("=" * 60)
            logger.info("TCS - Telescope Control Services")
            logger.info(f"Standalone Service: {service_type}.{args.variant}")
            if args.config_file:
                logger.info(f"Config: {args.config_file}")
            else:
                logger.info("Config: Using defaults (no config file)")
            logger.info("=" * 60)

        async def amain():
            """Async main function for service."""
            # Initialize ProcessContext (once per process)
            process_ctx = await ProcessContext.initialize(config_file=args.config_file)

            # Create ServiceRegistry from config
            registry = None
            if process_ctx.config_manager:
                raw_config = process_ctx.config_manager.get_raw_config()
                registry = ServiceRegistry(raw_config)

            # Create controller with new service_type + variant interface
            controller = ServiceController(
                service_type=service_type,
                variant=args.variant,
                registry=registry,
                runner_id=args.runner_id,
                parent_name=args.parent_name
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
        self.svc_logger.info("Permanent service started")

    async def stop_service(self):
        """Default implementation for permanent services - override for custom cleanup."""
        self.svc_logger.info("Permanent service stopping")


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
        self.svc_logger.info("Starting blocking permanent service")
        await self.on_start()

        # Start the main blocking task
        self._main_task = asyncio.create_task(self._run_wrapper())
        self.svc_logger.info("Blocking permanent service started")

    async def stop_service(self):
        """Stop the service and cancel the main task."""
        self.svc_logger.info("Stopping blocking permanent service")

        # Cancel the main task
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            self._main_task = None

        await self.on_stop()
        self.svc_logger.info("Blocking permanent service stopped")

    async def _run_wrapper(self):
        """Wrapper that handles the blocking run_service method."""
        try:
            await self.run_service()
        except asyncio.CancelledError:
            # Expected when service is stopped
            raise
        except Exception as e:
            self.svc_logger.error(f"Error in run_service: {e}")
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
        self.svc_logger.info("Single-shot service stopped")

    @abstractmethod
    async def execute(self):
        """Execute the single-shot task - override in subclasses."""
        pass

