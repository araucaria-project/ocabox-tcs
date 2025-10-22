"""Test external service discovery from external packages."""
import importlib

from ocabox_tcs.base_service import get_service_class


# Import external module to trigger decorator registration
importlib.import_module("tests.external_example.external_worker")


def test_external_service_registered():
    """Test that external service is registered with decorator."""
    # Service type is the filename stem (fallback when not in services/ directory)
    service_class = get_service_class("external_worker")
    assert service_class is not None
    assert service_class.__name__ == "ExternalWorkerService"


def test_external_service_is_blocking_permanent():
    """Test that external service uses correct base class."""
    from ocabox_tcs.base_service import BaseBlockingPermanentService

    service_class = get_service_class("external_worker")
    assert issubclass(service_class, BaseBlockingPermanentService)
