"""Test service discovery and decorator registration."""
import importlib

from ocabox_tcs.base_service import get_config_class, get_service_class


# Import modules to trigger decorator registration
importlib.import_module("ocabox_tcs.services.examples.01_minimal")
importlib.import_module("ocabox_tcs.services.examples.02_basic")
importlib.import_module("ocabox_tcs.services.hello_world")


def test_minimal_service_registered():
    """Test that minimal service is registered with decorator."""
    # Service type is now explicit: "examples.minimal"
    service_class = get_service_class("examples.minimal")
    assert service_class is not None
    assert service_class.__name__ == "MinimalService"


def test_basic_service_registered():
    """Test that basic service and config are registered."""
    # Service type is now explicit: "examples.basic"
    service_class = get_service_class("examples.basic")
    config_class = get_config_class("examples.basic")

    assert service_class is not None
    assert service_class.__name__ == "BasicService"

    assert config_class is not None
    assert config_class.__name__ == "BasicConfig"


def test_hello_world_registered():
    """Test that hello_world service is registered."""
    service_class = get_service_class("hello_world")
    assert service_class is not None
    assert service_class.__name__ == "HelloWorldService"
