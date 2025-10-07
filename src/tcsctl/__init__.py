"""tcsctl - Command-line interface and monitoring client for TCS services."""

from tcsctl.client import ServiceControlClient
from tcsctl.collector import ServiceInfo

__version__ = "0.3.3"

__all__ = ["ServiceControlClient", "ServiceInfo"]
