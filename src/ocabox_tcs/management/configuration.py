"""Configuration management with multiple sources and precedence."""

import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml
from serverish.messenger import Messenger


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR_NAME} environment variables in config.

    Pattern: ${VAR_NAME} - strict format (alphanumeric + underscore only)

    Behavior:
        - If VAR_NAME is set: Replace with environment variable value
        - If VAR_NAME is unset: Keep placeholder and log warning
        - If the entire value is ${VAR} and result is numeric, convert to int/float

    Examples:
        >>> os.environ["API_KEY"] = "secret123"
        >>> expand_env_vars("key: ${API_KEY}")
        'key: secret123'
        >>> os.environ["PORT"] = "4222"
        >>> expand_env_vars("${PORT}")  # Pure variable reference
        4222  # Auto-converted to int
        >>> expand_env_vars("key: ${MISSING}")  # MISSING not in env
        'key: ${MISSING}'  # Keeps placeholder, logs warning

    Args:
        value: Config value to expand (can be str, dict, list, or other types)

    Returns:
        Value with environment variables expanded (with type conversion for numbers)
    """
    if isinstance(value, str):
        # Pattern: ${VAR_NAME} - alphanumeric + underscore only
        pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'

        # Check if entire value is a single variable reference (for type conversion)
        full_match = re.fullmatch(pattern, value)
        if full_match:
            var_name = full_match.group(1)
            env_value = os.getenv(var_name)
            if env_value is None:
                logging.getLogger("cfg").warning(
                    f"Environment variable '${{{var_name}}}' not set, keeping placeholder"
                )
                return value  # Keep ${VAR_NAME}

            # Try to convert to int/float for pure variable references
            try:
                if '.' in env_value:
                    return float(env_value)
                else:
                    return int(env_value)
            except ValueError:
                # Not a number, return as string
                return env_value

        # Partial substitution in a string (e.g., "host:${PORT}")
        def replacer(match):
            var_name = match.group(1)
            env_value = os.getenv(var_name)
            if env_value is None:
                logging.getLogger("cfg").warning(
                    f"Environment variable '${{{var_name}}}' not set, keeping placeholder"
                )
                return match.group(0)  # Keep ${VAR_NAME}
            return env_value

        return re.sub(pattern, replacer, value)

    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]

    else:
        return value


class ConfigSource(ABC):
    """Base class for configuration sources."""
    
    def __init__(self, priority: int = 0):
        self.priority = priority  # Higher number = higher priority
    
    @abstractmethod
    def load(self) -> dict[str, Any]:
        """Load configuration data."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if source is available."""
        pass


class FileConfigSource(ConfigSource):
    """Configuration from YAML file."""

    def __init__(self, file_path: str | Path, priority: int = 10):
        super().__init__(priority)
        self.file_path = file_path

    def load(self) -> dict[str, Any]:
        """Load configuration from file and expand environment variables."""
        try:
            with open(self.file_path) as f:
                config = yaml.safe_load(f) or {}

            # Expand environment variables (${VAR_NAME} → os.getenv("VAR_NAME"))
            config = expand_env_vars(config)

            return config
        except Exception as e:
            logging.getLogger("cfg").warning(f"Failed to load config from {self.file_path}: {e}")
            return {}
    
    def is_available(self) -> bool:
        """Check if file exists."""
        return Path(self.file_path).exists()


class ArgsConfigSource(ConfigSource):
    """Configuration from command line arguments or dict."""
    
    def __init__(self, config_dict: dict[str, Any], priority: int = 30):
        super().__init__(priority)
        self.config_dict = config_dict
    
    def load(self) -> dict[str, Any]:
        """Return provided configuration."""
        return self.config_dict
    
    def is_available(self) -> bool:
        """Always available."""
        return True


class NATSConfigSource(ConfigSource):
    """Configuration from NATS (future implementation)."""
    
    def __init__(self, subject: str, messenger: Any = None, priority: int = 20):
        super().__init__(priority)
        self.subject = subject
        self.messenger = messenger
    
    def load(self) -> dict[str, Any]:
        """Load configuration from NATS (placeholder)."""
        # TODO: Implement NATS configuration loading
        logging.getLogger("cfg").warning("NATS configuration not yet implemented")
        return {}
    
    def is_available(self) -> bool:
        """Check if NATS messenger is available."""
        return self.messenger is not None


class DefaultConfigSource(ConfigSource):
    """Default configuration values."""
    
    def __init__(self, defaults: dict[str, Any] = None, priority: int = 0):
        super().__init__(priority)
        self.defaults = defaults or {}
    
    def load(self) -> dict[str, Any]:
        """Return default configuration."""
        return self.defaults
    
    def is_available(self) -> bool:
        """Always available."""
        return True


class ConfigurationManager:
    """Manages configuration from multiple sources with precedence."""
    
    def __init__(self):
        self.logger = logging.getLogger("cfg")
        self.sources: list[ConfigSource] = []
    
    def add_source(self, source: ConfigSource):
        """Add a configuration source."""
        self.sources.append(source)
        # Sort by priority (highest first)
        self.sources.sort(key=lambda s: s.priority, reverse=True)
        self.logger.debug(f"Added config source with priority {source.priority}")

    def log_sources(self):
        """Log all configuration sources and their availability."""
        if not self.sources:
            self.logger.info("Configuration sources: none")
            return

        self.logger.info("Configuration sources (priority order, highest first):")
        for source in self.sources:
            source_name = type(source).__name__.replace("ConfigSource", "")
            is_available = source.is_available()
            status = "✓ available" if is_available else "✗ unavailable"

            # Get source details
            details = ""
            if isinstance(source, FileConfigSource):
                details = f" ({source.file_path})"
            elif isinstance(source, NATSConfigSource):
                details = f" ({source.subject})"
            elif isinstance(source, ArgsConfigSource):
                details = " (command-line args)"

            self.logger.info(f"  [{source.priority:2d}] {source_name:15s} {status}{details}")
    
    def resolve_config(self, service_module: str | None = None, instance_id: str | None = None) -> dict[str, Any]:
        """Resolve configuration for a service from all sources.

        if service_module is None, return global configuration only."""
        merged_config = {}

        # Start with lowest priority sources and merge up
        for source in reversed(self.sources):
            if not source.is_available():
                continue

            try:
                source_config = source.load()
                if source_config:
                    # Look for service-specific config
                    service_config = self._extract_service_config(
                        source_config, service_module, instance_id
                    )
                    if service_config:
                        merged_config = self._deep_merge(merged_config, service_config)
                        self.logger.debug(f"Merged config from {type(source).__name__}")
            except Exception as e:
                self.logger.error(f"Error loading from {type(source).__name__}: {e}")

        return merged_config

    def get_raw_config(self) -> dict[str, Any]:
        """Get raw merged configuration from all sources without service filtering.

        Useful for launchers that need to access the services list.
        """
        merged_config = {}

        # Start with lowest priority sources and merge up
        for source in reversed(self.sources):
            if not source.is_available():
                continue

            try:
                source_config = source.load()
                if source_config:
                    merged_config = self._deep_merge(merged_config, source_config)
                    self.logger.debug(f"Merged raw config from {type(source).__name__}")
            except Exception as e:
                self.logger.error(f"Error loading from {type(source).__name__}: {e}")

        return merged_config
    
    def _extract_service_config(self, config: dict[str, Any],
                               module: str | None, instance: str | None) -> dict[str, Any]:
        """Extract service-specific configuration, or global if module is None."""
        service_config = {}

        # Look for exact match: services.module.instance
        if "services" in config and module and instance:
            services = config["services"]
            if isinstance(services, list):
                # List format: find matching service entry
                for service_entry in services:
                    if (service_entry.get("type") == module.split(".")[-1] and
                        service_entry.get("instance_context") == instance):
                        service_config.update(service_entry)
            elif isinstance(services, dict):
                # Dict format: hierarchical lookup
                module_name = module.split(".")[-1]
                if module_name in services:
                    module_config = services[module_name]
                    if isinstance(module_config, dict) and instance in module_config:
                        service_config.update(module_config[instance])
                    elif not isinstance(module_config, dict):
                        service_config.update(module_config)

        # Also include global config
        global_config = {k: v for k, v in config.items() if k != "services"}
        if global_config:
            service_config = self._deep_merge(global_config, service_config)

        return service_config
    
    def _deep_merge(self, base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dictionaries."""
        result = base.copy()
        
        for key, value in update.items():
            if (key in result and 
                isinstance(result[key], dict) and 
                isinstance(value, dict)):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        
        return result


async def create_configuration_manager(
    config_file: str | Path | None = None,
    args_config: dict[str, Any] | None = None,
    config_subject: str | None = None,
    config_messenger: Any = None,
    defaults: dict[str, Any] | None = None
) -> ConfigurationManager:
    """Create a configuration manager with standard sources."""
    manager = ConfigurationManager()
    
    # Add default config
    if defaults:
        manager.add_source(DefaultConfigSource(defaults))
    
    # Add file config
    if config_file:
        manager.add_source(FileConfigSource(config_file))

    # check for NATS config in already added sources
    if config_subject and config_messenger is None:
        if args_config:
            manager.add_source(ArgsConfigSource(args_config)) # may contain nats config

        try:
            global_config = manager.resolve_config()
            nats_host = global_config.get("nats", {}).get("host", "localhost")
            nats_port = global_config.get("nats", {}).get("port", 4222)
            config_messenger = Messenger(host=nats_host, port=nats_port)
            if not config_messenger.is_open:
                await config_messenger.open(host=nats_host, port=nats_port)
        except Exception as e:
            logging.getLogger('config').warning(f"Failed to create NATS messenger for config: {e}")
            config_messenger = None

    # Add NATS config  
    if config_subject and config_messenger:
        manager.add_source(NATSConfigSource(config_subject, config_messenger))
    
    # Add args config - highest priority
    if args_config:
        manager.add_source(ArgsConfigSource(args_config))

    return manager