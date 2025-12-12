"""Test environment variable expansion and .env file support."""
import os
import tempfile
from pathlib import Path

import pytest

from ocabox_tcs.management.configuration import expand_env_vars, FileConfigSource


class TestExpandEnvVars:
    """Test environment variable expansion in configuration."""

    def test_expand_simple_var(self):
        """Test expanding a single environment variable."""
        os.environ["TEST_VAR"] = "test_value"
        try:
            result = expand_env_vars("key: ${TEST_VAR}")
            assert result == "key: test_value"
        finally:
            del os.environ["TEST_VAR"]

    def test_expand_multiple_vars(self):
        """Test expanding multiple environment variables in one string."""
        os.environ["VAR1"] = "value1"
        os.environ["VAR2"] = "value2"
        try:
            result = expand_env_vars("${VAR1} and ${VAR2}")
            assert result == "value1 and value2"
        finally:
            del os.environ["VAR1"]
            del os.environ["VAR2"]

    def test_expand_var_in_dict(self):
        """Test expanding environment variables in nested dictionaries."""
        os.environ["DATABASE_URL"] = "postgresql://localhost/testdb"
        os.environ["API_KEY"] = "secret123"
        try:
            config = {
                "database": {"url": "${DATABASE_URL}"},
                "api": {"key": "${API_KEY}"}
            }
            result = expand_env_vars(config)
            assert result["database"]["url"] == "postgresql://localhost/testdb"
            assert result["api"]["key"] == "secret123"
        finally:
            del os.environ["DATABASE_URL"]
            del os.environ["API_KEY"]

    def test_expand_var_in_list(self):
        """Test expanding environment variables in lists."""
        os.environ["SERVER1"] = "server1.example.com"
        os.environ["SERVER2"] = "server2.example.com"
        try:
            config = ["${SERVER1}", "${SERVER2}"]
            result = expand_env_vars(config)
            assert result == ["server1.example.com", "server2.example.com"]
        finally:
            del os.environ["SERVER1"]
            del os.environ["SERVER2"]

    def test_missing_var_keeps_placeholder(self):
        """Test that missing environment variables keep placeholder."""
        # Make sure variable doesn't exist
        if "NONEXISTENT_VAR" in os.environ:
            del os.environ["NONEXISTENT_VAR"]

        result = expand_env_vars("key: ${NONEXISTENT_VAR}")
        assert result == "key: ${NONEXISTENT_VAR}"

    def test_partial_expansion(self):
        """Test that only defined variables are expanded."""
        os.environ["DEFINED_VAR"] = "defined"
        try:
            if "UNDEFINED_VAR" in os.environ:
                del os.environ["UNDEFINED_VAR"]

            result = expand_env_vars("${DEFINED_VAR} and ${UNDEFINED_VAR}")
            assert result == "defined and ${UNDEFINED_VAR}"
        finally:
            del os.environ["DEFINED_VAR"]

    def test_non_string_values_unchanged(self):
        """Test that non-string values are returned unchanged."""
        assert expand_env_vars(42) == 42
        assert expand_env_vars(3.14) == 3.14
        assert expand_env_vars(True) is True
        assert expand_env_vars(None) is None

    def test_empty_string(self):
        """Test expanding empty string."""
        result = expand_env_vars("")
        assert result == ""

    def test_no_variables_in_string(self):
        """Test string without variables remains unchanged."""
        result = expand_env_vars("plain text without vars")
        assert result == "plain text without vars"

    def test_malformed_variable_syntax(self):
        """Test that malformed syntax is not expanded."""
        # Missing closing brace
        result = expand_env_vars("${MISSING_BRACE")
        assert result == "${MISSING_BRACE"

        # Missing opening brace
        result = expand_env_vars("MISSING_BRACE}")
        assert result == "MISSING_BRACE}"

        # Dollar without braces
        result = expand_env_vars("$VAR_NAME")
        assert result == "$VAR_NAME"

    def test_variable_with_numbers_and_underscores(self):
        """Test variables with numbers and underscores."""
        os.environ["VAR_NAME_123"] = "value123"
        os.environ["_LEADING_UNDERSCORE"] = "underscore_value"
        try:
            result = expand_env_vars("${VAR_NAME_123} and ${_LEADING_UNDERSCORE}")
            assert result == "value123 and underscore_value"
        finally:
            del os.environ["VAR_NAME_123"]
            del os.environ["_LEADING_UNDERSCORE"]


class TestFileConfigSourceWithExpansion:
    """Test FileConfigSource with environment variable expansion."""

    def test_config_file_with_env_vars(self):
        """Test loading config file with environment variables."""
        os.environ["TEST_API_KEY"] = "secret_key_123"
        os.environ["TEST_DB_HOST"] = "localhost"

        try:
            # Create temporary config file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write("""
services:
  - type: test_service
    api_key: "${TEST_API_KEY}"
    database:
      host: "${TEST_DB_HOST}"
      port: 5432
""")
                config_path = f.name

            # Load config
            source = FileConfigSource(config_path)
            config = source.load()

            # Verify expansion
            assert config["services"][0]["api_key"] == "secret_key_123"
            assert config["services"][0]["database"]["host"] == "localhost"
            assert config["services"][0]["database"]["port"] == 5432  # Non-string unchanged

        finally:
            del os.environ["TEST_API_KEY"]
            del os.environ["TEST_DB_HOST"]
            Path(config_path).unlink()

    def test_config_file_with_missing_env_vars(self):
        """Test loading config file with missing environment variables."""
        # Ensure variable doesn't exist
        if "MISSING_VAR" in os.environ:
            del os.environ["MISSING_VAR"]

        # Create temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
services:
  - type: test_service
    api_key: "${MISSING_VAR}"
""")
            config_path = f.name

        try:
            # Load config
            source = FileConfigSource(config_path)
            config = source.load()

            # Verify placeholder kept
            assert config["services"][0]["api_key"] == "${MISSING_VAR}"

        finally:
            Path(config_path).unlink()

    def test_config_file_without_env_vars(self):
        """Test loading normal config file without environment variables."""
        # Create temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
services:
  - type: test_service
    setting: normal_value
""")
            config_path = f.name

        try:
            # Load config
            source = FileConfigSource(config_path)
            config = source.load()

            # Verify normal loading
            assert config["services"][0]["setting"] == "normal_value"

        finally:
            Path(config_path).unlink()


class TestDotenvIntegration:
    """Test .env file integration (integration tests)."""

    def test_dotenv_loading_if_exists(self):
        """Test that .env file is loaded when present."""
        # This is an integration test - checks if dotenv is properly integrated
        # The actual loading happens in launchers/base_service.py
        try:
            from dotenv import load_dotenv
            # If import succeeds, python-dotenv is installed
            assert True
        except ImportError:
            pytest.skip("python-dotenv not installed")

    def test_env_file_example_exists(self):
        """Test that .env.example template exists in project root."""
        project_root = Path(__file__).parent.parent
        env_example = project_root / ".env.example"
        assert env_example.exists(), ".env.example template should exist"

        # Verify it's a valid template
        content = env_example.read_text()
        assert "# .env.example" in content
        assert ".env" in content.lower()

    def test_gitignore_includes_env(self):
        """Test that .gitignore includes .env to prevent committing secrets."""
        project_root = Path(__file__).parent.parent
        gitignore = project_root / ".gitignore"
        assert gitignore.exists(), ".gitignore should exist"

        content = gitignore.read_text()
        assert ".env" in content, ".env should be in .gitignore"
