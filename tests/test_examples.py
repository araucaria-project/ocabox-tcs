"""Basic tests for example services."""
import importlib

import pytest


# Import modules that start with numbers using importlib
minimal_module = importlib.import_module("ocabox_tcs.services.examples.01_minimal")
basic_module = importlib.import_module("ocabox_tcs.services.examples.02_basic")
logging_module = importlib.import_module("ocabox_tcs.services.examples.03_logging")
monitoring_module = importlib.import_module("ocabox_tcs.services.examples.04_monitoring")

MinimalService = minimal_module.MinimalService
BasicService = basic_module.BasicService
BasicConfig = basic_module.BasicConfig
LoggingService = logging_module.LoggingService
MonitoringService = monitoring_module.MonitoringService


def test_minimal_service_class_exists():
    """Test that MinimalService can be imported and instantiated."""
    assert MinimalService is not None
    service = MinimalService()
    assert service is not None


def test_basic_service_class_exists():
    """Test that BasicService can be imported and instantiated."""
    assert BasicService is not None
    service = BasicService()
    assert service is not None


def test_basic_config_defaults():
    """Test BasicConfig default values."""
    config = BasicConfig(type="basic", instance_context="test")
    assert config.interval == 3.0
    assert config.message == "Hello from basic service"


def test_logging_service_class_exists():
    """Test that LoggingService can be imported and instantiated."""
    assert LoggingService is not None
    service = LoggingService()
    assert service is not None


def test_monitoring_service_class_exists():
    """Test that MonitoringService can be imported and instantiated."""
    assert MonitoringService is not None
    service = MonitoringService()
    assert service is not None


@pytest.mark.asyncio
async def test_minimal_service_has_run_service():
    """Test that MinimalService has required run_service method."""
    service = MinimalService()
    assert hasattr(service, 'run_service')
    assert callable(service.run_service)


@pytest.mark.asyncio
async def test_monitoring_service_has_healthcheck():
    """Test that MonitoringService has healthcheck method."""
    service = MonitoringService()
    assert hasattr(service, 'healthcheck')
    assert callable(service.healthcheck)
