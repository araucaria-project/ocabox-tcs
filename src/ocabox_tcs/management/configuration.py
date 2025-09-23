"""Configuration management with multiple sources and precedence."""

import os
import yaml
import logging
from typing import Dict, Any, Optional, List, Union
from pathlib import Path
from abc import ABC, abstractmethod


class ConfigSource(ABC):
    """Base class for configuration sources."""
    
    def __init__(self, priority: int = 0):
        self.priority = priority  # Higher number = higher priority
    
    @abstractmethod
    def load(self) -> Dict[str, Any]:
        """Load configuration data."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if source is available."""
        pass


class FileConfigSource(ConfigSource):
    """Configuration from YAML file."""
    
    def __init__(self, file_path: Union[str, Path], priority: int = 10):
        super().__init__(priority)
        self.file_path = file_path
    
    def load(self) -> Dict[str, Any]:
        """Load configuration from file."""
        try:
            with open(self.file_path, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logging.getLogger(__name__).warning(f"Failed to load config from {self.file_path}: {e}")
            return {}
    
    def is_available(self) -> bool:
        """Check if file exists."""
        return Path(self.file_path).exists()


class ArgsConfigSource(ConfigSource):
    """Configuration from command line arguments or dict."""
    
    def __init__(self, config_dict: Dict[str, Any], priority: int = 30):
        super().__init__(priority)
        self.config_dict = config_dict
    
    def load(self) -> Dict[str, Any]:
        """Return provided configuration."""
        return self.config_dict
    
    def is_available(self) -> bool:
        """Always available."""
        return True


class NATSConfigSource(ConfigSource):
    """Configuration from NATS (future implementation)."""
    
    def __init__(self, topic: str, messenger: Any = None, priority: int = 20):
        super().__init__(priority)
        self.topic = topic
        self.messenger = messenger
    
    def load(self) -> Dict[str, Any]:
        """Load configuration from NATS (placeholder)."""
        # TODO: Implement NATS configuration loading
        logging.getLogger(__name__).warning("NATS configuration not yet implemented")
        return {}
    
    def is_available(self) -> bool:
        """Check if NATS messenger is available."""
        return self.messenger is not None


class DefaultConfigSource(ConfigSource):
    """Default configuration values."""
    
    def __init__(self, defaults: Dict[str, Any] = None, priority: int = 0):
        super().__init__(priority)
        self.defaults = defaults or {}
    
    def load(self) -> Dict[str, Any]:
        """Return default configuration."""
        return self.defaults
    
    def is_available(self) -> bool:
        """Always available."""
        return True


class ConfigurationManager:
    """Manages configuration from multiple sources with precedence."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.sources: List[ConfigSource] = []
    
    def add_source(self, source: ConfigSource):
        """Add a configuration source."""
        self.sources.append(source)
        # Sort by priority (highest first)
        self.sources.sort(key=lambda s: s.priority, reverse=True)
        self.logger.debug(f"Added config source with priority {source.priority}")
    
    def resolve_config(self, service_module: str, instance_id: str) -> Dict[str, Any]:
        """Resolve configuration for a service from all sources."""
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
    
    def _extract_service_config(self, config: Dict[str, Any], 
                               module: str, instance: str) -> Dict[str, Any]:
        """Extract service-specific configuration."""
        service_config = {}
        
        # Look for exact match: services.module.instance
        if "services" in config:
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
    
    def _deep_merge(self, base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
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


def create_configuration_manager(
    config_file: Optional[Union[str, Path]] = None,
    args_config: Optional[Dict[str, Any]] = None,
    nats_topic: Optional[str] = None,
    messenger: Any = None,
    defaults: Optional[Dict[str, Any]] = None
) -> ConfigurationManager:
    """Create a configuration manager with standard sources."""
    manager = ConfigurationManager()
    
    # Add default config
    if defaults:
        manager.add_source(DefaultConfigSource(defaults))
    
    # Add file config
    if config_file:
        manager.add_source(FileConfigSource(config_file))
    
    # Add NATS config  
    if nats_topic and messenger:
        manager.add_source(NATSConfigSource(nats_topic, messenger))
    
    # Add args config (highest priority)
    if args_config:
        manager.add_source(ArgsConfigSource(args_config))
    
    return manager