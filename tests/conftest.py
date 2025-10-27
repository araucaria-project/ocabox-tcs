"""Pytest configuration for ocabox-tcs tests.

This file makes fixtures from tests/fixtures/ available to all tests.
"""

# Import fixtures to make them discoverable by pytest
from tests.fixtures.nats_fixtures import nats_server, nats_client, nats_url

# Make fixtures available
__all__ = [
    "nats_server",
    "nats_client",
    "nats_url",
]
