"""Service Registry for TCS service discovery.

The ServiceRegistry maps service_type to Python module paths and handles
service class discovery. It replaces the old path-parsing-based discovery
with a clean, explicit registry system.

Usage:
    registry = ServiceRegistry(config)
    service_class = registry.get_service_class('hello_world')
    module_path = registry.resolve_module('halina.server')
"""

import importlib
import logging
from typing import Any

from ocabox_tcs.base_service import BaseService, get_service_class

_log = logging.getLogger("svc.registry")


class ServiceRegistryError(Exception):
    """Base exception for service registry errors."""
    pass


class ServiceTypeNotFoundError(ServiceRegistryError):
    """Raised when a service type is not found in the registry."""
    pass


class ServiceClassNotFoundError(ServiceRegistryError):
    """Raised when a service class is not found after module import."""
    pass


class ServiceRegistry:
    """Registry for service type to module path mapping.

    The registry provides:
    1. Explicit mapping from service_type to Python module path
    2. Shorthand for internal TCS services (null value means ocabox_tcs.services.{type})
    3. Service class discovery via module import and decorator lookup

    Configuration format (services.yaml):

        registry:
          hello_world: ~                              # -> ocabox_tcs.services.hello_world
          examples.minimal: ~                         # -> ocabox_tcs.services.examples.minimal
          halina.server: halina.server.halina_server  # external package

    Attributes:
        registry: Dict mapping service_type to module_path (or None for internal)
        _loaded_modules: Set of already imported modules
    """

    # Default module prefix for internal TCS services
    DEFAULT_MODULE_PREFIX = "ocabox_tcs.services"

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the ServiceRegistry.

        Args:
            config: Configuration dict containing optional 'registry' section.
                   If None or missing registry section, uses empty registry
                   (all types will use default prefix).
        """
        self.registry: dict[str, str | None] = {}
        self._loaded_modules: set[str] = set()

        if config is not None:
            self.registry = config.get("registry", {}) or {}

        _log.debug(f"ServiceRegistry initialized with {len(self.registry)} entries")

    def resolve_module(self, service_type: str) -> str:
        """Resolve service type to Python module path.

        Resolution rules:
        1. If type is in registry with non-null value: use that module path
        2. If type is in registry with null value: use default prefix
        3. If type not in registry: use default prefix (fallback for convenience)

        Args:
            service_type: The service type identifier (e.g., 'hello_world', 'halina.server')

        Returns:
            Python module path (e.g., 'ocabox_tcs.services.hello_world')

        Note:
            For service types with dots (e.g., 'examples.minimal'), the dots are
            converted to path separators in the default prefix case:
            'examples.minimal' -> 'ocabox_tcs.services.examples.minimal'
        """
        if service_type in self.registry:
            module_path = self.registry[service_type]
            if module_path is None:
                # Null value: use default prefix
                module_path = f"{self.DEFAULT_MODULE_PREFIX}.{service_type}"
                _log.debug(f"Resolved '{service_type}' via registry (null) -> '{module_path}'")
            else:
                _log.debug(f"Resolved '{service_type}' via registry -> '{module_path}'")
        else:
            # Not in registry: use default prefix (convenience fallback)
            module_path = f"{self.DEFAULT_MODULE_PREFIX}.{service_type}"
            _log.debug(f"Resolved '{service_type}' via default -> '{module_path}'")

        return module_path

    def get_service_class(self, service_type: str) -> type[BaseService]:
        """Get service class by type.

        This method:
        1. Resolves the module path from the registry
        2. Imports the module (triggers @service decorator registration)
        3. Looks up the class in the global decorator registry

        Args:
            service_type: The service type identifier

        Returns:
            The service class decorated with @service(service_type)

        Raises:
            ServiceClassNotFoundError: If no @service decorated class found
            ImportError: If module cannot be imported
        """
        # Resolve module path
        module_path = self.resolve_module(service_type)

        # Import module if not already loaded
        if module_path not in self._loaded_modules:
            _log.debug(f"Importing module '{module_path}' for service type '{service_type}'")
            try:
                importlib.import_module(module_path)
                self._loaded_modules.add(module_path)
            except ImportError as e:
                _log.error(f"Failed to import module '{module_path}': {e}")
                raise

        # Look up in global decorator registry
        service_class = get_service_class(service_type)

        if service_class is None:
            raise ServiceClassNotFoundError(
                f"Module '{module_path}' was imported but no @service('{service_type}') "
                f"decorated class was found. Ensure your service class has the decorator: "
                f"@service('{service_type}')"
            )

        return service_class

    def has_type(self, service_type: str) -> bool:
        """Check if a service type is explicitly registered.

        Args:
            service_type: The service type identifier

        Returns:
            True if type is in the registry (even if value is null)
        """
        return service_type in self.registry

    def list_registered_types(self) -> list[str]:
        """List all explicitly registered service types.

        Returns:
            List of service type identifiers in the registry
        """
        return list(self.registry.keys())

    def add_type(self, service_type: str, module_path: str | None = None) -> None:
        """Add a service type to the registry.

        Args:
            service_type: The service type identifier
            module_path: Python module path, or None for default prefix
        """
        self.registry[service_type] = module_path
        _log.debug(f"Added registry entry: '{service_type}' -> '{module_path}'")


def validate_variant(variant: str) -> None:
    """Validate that a variant identifier contains no dots.

    The variant (instance identifier) is always the last segment of the
    service_id (e.g., 'hello_world.dev' has variant 'dev'). To ensure
    unambiguous parsing, variants cannot contain dots.

    Args:
        variant: The variant identifier to validate

    Raises:
        ValueError: If variant contains dots
    """
    if '.' in variant:
        raise ValueError(
            f"Variant '{variant}' contains dots, which is not allowed. "
            f"The variant is the last segment of the service_id and must not contain dots. "
            f"Use a different identifier without dots."
        )


def parse_service_id(service_id: str) -> tuple[str, str]:
    """Parse a service_id into service_type and variant.

    The service_id format is: {service_type}.{variant}
    where variant is always the last dot-separated segment.

    Examples:
        'hello_world.dev' -> ('hello_world', 'dev')
        'examples.minimal.tutorial' -> ('examples.minimal', 'tutorial')
        'halina.server.prod' -> ('halina.server', 'prod')

    Args:
        service_id: The full service identifier

    Returns:
        Tuple of (service_type, variant)

    Raises:
        ValueError: If service_id has no dots (missing variant)
    """
    if '.' not in service_id:
        raise ValueError(
            f"Invalid service_id '{service_id}': must contain at least one dot. "
            f"Format is {{service_type}}.{{variant}}, e.g., 'hello_world.dev'"
        )

    # Split on last dot to get variant
    last_dot = service_id.rfind('.')
    service_type = service_id[:last_dot]
    variant = service_id[last_dot + 1:]

    return service_type, variant


def build_service_id(service_type: str, variant: str) -> str:
    """Build a service_id from service_type and variant.

    Args:
        service_type: The service type identifier
        variant: The variant identifier (must not contain dots)

    Returns:
        The full service_id in format: {service_type}.{variant}

    Raises:
        ValueError: If variant contains dots
    """
    validate_variant(variant)
    return f"{service_type}.{variant}"
