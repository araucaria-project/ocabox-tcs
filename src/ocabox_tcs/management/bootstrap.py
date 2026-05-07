"""Bootstrap helpers shared between launchers and CLI tools.

Single source of truth for:
- Config file path resolution (default vs. explicit-must-exist semantics)
- NATS connection settings (config -> env -> defaults; explicit overrides win)

Both launcher entry points (`BaseLauncher.launch`) and CLI tools (`tcsctl`)
use these helpers so the resolution rules stay in one place.
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ocabox_tcs.management.configuration import ConfigurationManager


DEFAULT_CONFIG_FILE = "config/services.yaml"
DEFAULT_NATS_HOST = "localhost"
DEFAULT_NATS_PORT = 4222
DEFAULT_SUBJECT_PREFIX = "svc"


@dataclass(frozen=True)
class NatsSettings:
    """Resolved NATS connection settings."""

    host: str
    port: int
    subject_prefix: str
    required: bool = True


def determine_config_file(
    config_arg: str | None,
    default: str = DEFAULT_CONFIG_FILE,
    logger: logging.Logger | None = None,
) -> str:
    """Resolve config file path.

    If `config_arg` is given, the file MUST exist (calls `sys.exit(1)` otherwise).
    If `config_arg` is None, returns the default path. The default may not exist;
    callers should pass it through `FileConfigSource`, which is no-op for missing
    files.

    Args:
        config_arg: Explicit config path from CLI (or None for default)
        default: Default config file path
        logger: Logger to use (default: 'launch')

    Returns:
        Path to config file (may not exist if default was used)
    """
    log = logger or logging.getLogger("launch")

    if config_arg is not None:
        if not Path(config_arg).exists():
            log.error(f"Configuration file not found: {config_arg}")
            log.error("Explicitly provided config file must exist. Exiting.")
            sys.exit(1)
        log.info(f"Using config file: {config_arg}")
        return config_arg

    if Path(default).exists():
        log.info(f"Using default config file: {default}")
    else:
        log.info(f"Default config file not found: {default}")
        log.info("Continuing with empty configuration")
    return default


def resolve_nats_settings(
    config_manager: ConfigurationManager | None,
    host_override: str | None = None,
    port_override: int | None = None,
    subject_prefix_override: str | None = None,
) -> NatsSettings:
    """Resolve NATS connection settings from layered sources.

    Resolution order (highest precedence first):
        1. Explicit overrides (e.g., from CLI args)
        2. Config manager 'nats:' section (if available)
        3. Environment variables `NATS_HOST` / `NATS_PORT`
        4. Hardcoded defaults

    `subject_prefix` has no env-var fallback (less common to override per-shell).
    `required` comes from config only; CLI tools typically ignore it.

    Args:
        config_manager: Optional ConfigurationManager to read 'nats:' section
        host_override: Explicit host override
        port_override: Explicit port override
        subject_prefix_override: Explicit subject prefix override

    Returns:
        Resolved `NatsSettings`.

    Raises:
        ValueError: If port cannot be converted to int
    """
    nats_section: dict = {}
    if config_manager is not None:
        global_config = config_manager.resolve_config()
        nats_section = global_config.get("nats") or {}

    host = host_override
    if host is None:
        host = nats_section.get("host")
    if host is None:
        host = os.getenv("NATS_HOST", DEFAULT_NATS_HOST)

    port_raw: str | int | None = port_override
    if port_raw is None:
        port_raw = nats_section.get("port")
    if port_raw is None:
        port_raw = os.getenv("NATS_PORT", DEFAULT_NATS_PORT)
    try:
        port = int(port_raw)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid NATS port configuration: '{port_raw}' cannot be converted to integer"
        ) from e

    subject_prefix = (
        subject_prefix_override
        or nats_section.get("subject_prefix")
        or DEFAULT_SUBJECT_PREFIX
    )

    required = bool(nats_section.get("required", True))

    return NatsSettings(
        host=host,
        port=port,
        subject_prefix=subject_prefix,
        required=required,
    )
