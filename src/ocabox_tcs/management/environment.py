"""Environment variable management utilities.

This module provides utilities for loading environment variables from .env files
and managing environment configuration for TCS services and launchers.
"""

import logging
from pathlib import Path


def load_dotenv_if_available() -> tuple[bool, Path | None]:
    """Load .env file from current directory if it exists.

    Loads environment variables from .env file using python-dotenv library.
    Existing environment variables take precedence (override=False).

    Returns:
        Tuple of (success: bool, env_file_path: Path | None)
        - success: True if .env file was loaded, False otherwise
        - env_file_path: Absolute path to .env file if loaded, None otherwise

    Notes:
        - Silently returns False if python-dotenv is not installed
        - Silently returns False if .env file does not exist
        - Existing environment variables are never overwritten (override=False)
    """
    logger = logging.getLogger("env")

    try:
        from dotenv import load_dotenv

        env_file = Path(".env")
        if env_file.exists():
            load_dotenv(override=False)  # Existing env vars take precedence
            return True, env_file.absolute()
        else:
            logger.debug("No .env file found in current directory")
            return False, None

    except ImportError:
        logger.debug("python-dotenv not installed, skipping .env file loading")
        return False, None
