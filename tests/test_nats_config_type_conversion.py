"""Test NATS configuration type conversion for port values."""
import os
import tempfile
from pathlib import Path

import pytest

from ocabox_tcs.management.configuration import FileConfigSource, expand_env_vars


class TestNatsConfigTypeConversion:
    """Test that NATS config handles port type conversion correctly."""

    def test_nats_port_from_env_var_with_default(self):
        """Test NATS port with ${VAR:-default} syntax when env var not set."""
        # Ensure env vars don't exist
        if "NATS_HOST" in os.environ:
            del os.environ["NATS_HOST"]
        if "NATS_PORT" in os.environ:
            del os.environ["NATS_PORT"]

        # Create config file with bash-style defaults (like halina9000)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
nats:
  host: ${NATS_HOST:-nats.oca.lan}
  port: ${NATS_PORT:-4222}
  subject_prefix: svc
""")
            config_path = f.name

        try:
            # Load config
            source = FileConfigSource(config_path)
            config = source.load()

            # Verify that port is an integer, not a string
            assert config["nats"]["host"] == "nats.oca.lan"
            assert config["nats"]["port"] == 4222
            assert isinstance(config["nats"]["port"], int), (
                f"Port should be int, got {type(config['nats']['port'])}"
            )

        finally:
            Path(config_path).unlink()

    def test_nats_port_from_env_var(self):
        """Test NATS port when env var is set."""
        os.environ["NATS_HOST"] = "custom.nats.server"
        os.environ["NATS_PORT"] = "9999"

        try:
            # Create config file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write("""
nats:
  host: ${NATS_HOST:-nats.oca.lan}
  port: ${NATS_PORT:-4222}
""")
                config_path = f.name

            # Load config
            source = FileConfigSource(config_path)
            config = source.load()

            # Verify that env var values are used and port is an integer
            assert config["nats"]["host"] == "custom.nats.server"
            assert config["nats"]["port"] == 9999
            assert isinstance(config["nats"]["port"], int)

        finally:
            del os.environ["NATS_HOST"]
            del os.environ["NATS_PORT"]
            Path(config_path).unlink()

    def test_direct_expand_env_vars_with_nats_config(self):
        """Test expand_env_vars directly with NATS config structure."""
        # Ensure env vars don't exist
        if "NATS_HOST" in os.environ:
            del os.environ["NATS_HOST"]
        if "NATS_PORT" in os.environ:
            del os.environ["NATS_PORT"]

        # Simulate YAML parse result (strings before expansion)
        config = {
            "nats": {
                "host": "${NATS_HOST:-nats.oca.lan}",
                "port": "${NATS_PORT:-4222}",
                "subject_prefix": "svc"
            }
        }

        # Expand env vars
        expanded = expand_env_vars(config)

        # Verify types
        assert expanded["nats"]["host"] == "nats.oca.lan"
        assert expanded["nats"]["port"] == 4222
        assert isinstance(expanded["nats"]["port"], int), (
            "Port must be integer for serverish library compatibility"
        )
        assert expanded["nats"]["subject_prefix"] == "svc"

    def test_invalid_port_value_in_env_var(self):
        """Test that invalid port values in env vars are handled."""
        os.environ["NATS_PORT"] = "not_a_number"

        try:
            config = {"port": "${NATS_PORT}"}
            expanded = expand_env_vars(config)

            # Should stay as string since it can't be converted
            assert expanded["port"] == "not_a_number"
            assert isinstance(expanded["port"], str)

        finally:
            del os.environ["NATS_PORT"]
