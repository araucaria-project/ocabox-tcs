"""Configuration generator for tests.

Generates launcher configuration files from service scenarios.
Supports all launcher types and service configurations.
"""

import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from tests.helpers.launcher_harness import ServiceScenario


class ConfigGenerator:
    """Generates launcher configuration files for testing.

    Creates YAML configuration files from service scenarios.
    Supports temporary files or persistent config files.

    Args:
        launcher_type: Launcher type ("process", "asyncio", "systemd", "container")
        nats_host: NATS server host
        nats_port: NATS server port
        subject_prefix: NATS subject prefix (default: "svc")
    """

    def __init__(
        self,
        launcher_type: str = "process",
        nats_host: str = "localhost",
        nats_port: int = 4222,
        subject_prefix: str = "test.svc"
    ):
        self.launcher_type = launcher_type
        self.nats_host = nats_host
        self.nats_port = nats_port
        self.subject_prefix = subject_prefix
        self._temp_files: list[Path] = []

    def generate_config(
        self,
        scenarios: list[ServiceScenario],
        output_path: Path | str | None = None,
        temp: bool = True
    ) -> Path:
        """Generate launcher configuration file from scenarios.

        Args:
            scenarios: List of service scenarios to include
            output_path: Output file path (if temp=False)
            temp: Create temporary file (default: True)

        Returns:
            Path to generated configuration file
        """
        config = self._build_config(scenarios)

        # Determine output path
        if temp:
            # Create temporary file
            fd, path = tempfile.mkstemp(suffix=".yaml", prefix="ocabox-test-")
            try:
                # Close the file descriptor before writing via Path
                import os
                os.close(fd)
            except OSError:
                pass  # Already closed
            output_path = Path(path)
            self._temp_files.append(output_path)
        else:
            if output_path is None:
                raise ValueError("output_path required when temp=False")
            output_path = Path(output_path)

        # Write YAML configuration
        output_path.write_text(yaml.dump(config, default_flow_style=False))
        return output_path

    def _build_config(self, scenarios: list[ServiceScenario]) -> dict[str, Any]:
        """Build configuration dictionary from scenarios.

        Args:
            scenarios: List of service scenarios

        Returns:
            Configuration dictionary
        """
        config = {
            "nats": {
                "host": self.nats_host,
                "port": self.nats_port,
                "subject_prefix": self.subject_prefix,
            },
            "services": []
        }

        # Convert each scenario to service config
        for scenario in scenarios:
            service_config = {
                "type": scenario.service_type,
                "instance_context": scenario.instance_context,
            }

            # Add module path for test services (they're in tests.services, not ocabox_tcs.services)
            if scenario.service_type.startswith("mock_"):
                service_config["module"] = f"tests.services.{scenario.service_type}"

            # Add restart policy if not default
            if scenario.restart != "no":
                service_config["restart"] = scenario.restart
                service_config["restart_sec"] = scenario.restart_sec
                if scenario.restart_max > 0:
                    service_config["restart_max"] = scenario.restart_max
                    service_config["restart_window"] = scenario.restart_window

            # Add service-specific config fields at top level (not nested)
            # The filtering mechanism in ServiceController will extract only the fields
            # that the service's config class accepts
            if scenario.config:
                service_config.update(scenario.config)

            config["services"].append(service_config)

        return config

    def cleanup(self) -> None:
        """Remove all temporary configuration files."""
        for path in self._temp_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._temp_files.clear()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup temp files."""
        self.cleanup()
        return False


def create_simple_config(
    service_type: str,
    instance_context: str,
    restart: str = "no",
    nats_host: str = "localhost",
    nats_port: int = 4222,
    config: dict[str, Any] | None = None,
    temp: bool = True,
    output_path: Path | str | None = None
) -> Path:
    """Helper function to create simple single-service config.

    Args:
        service_type: Service type name
        instance_context: Instance context identifier
        restart: Restart policy (default: "no")
        nats_host: NATS server host (default: "localhost")
        nats_port: NATS server port (default: 4222)
        config: Service-specific configuration (default: None)
        temp: Create temporary file (default: True)
        output_path: Output file path (if temp=False)

    Returns:
        Path to generated configuration file
    """
    scenario = ServiceScenario(
        service_type=service_type,
        instance_context=instance_context,
        restart=restart,
        config=config
    )

    generator = ConfigGenerator(nats_host=nats_host, nats_port=nats_port)
    return generator.generate_config([scenario], output_path=output_path, temp=temp)


def create_multi_service_config(
    scenarios: list[ServiceScenario],
    nats_host: str = "localhost",
    nats_port: int = 4222,
    temp: bool = True,
    output_path: Path | str | None = None
) -> Path:
    """Helper function to create multi-service config.

    Args:
        scenarios: List of service scenarios
        nats_host: NATS server host (default: "localhost")
        nats_port: NATS server port (default: 4222)
        temp: Create temporary file (default: True)
        output_path: Output file path (if temp=False)

    Returns:
        Path to generated configuration file
    """
    generator = ConfigGenerator(nats_host=nats_host, nats_port=nats_port)
    return generator.generate_config(scenarios, output_path=output_path, temp=temp)


def create_crash_test_config(
    restart_policy: str,
    crash_delay: float = 0.5,
    exit_code: int = 1,
    nats_host: str = "localhost",
    nats_port: int = 4222,
    restart_max: int = 0,
    temp: bool = True,
    output_path: Path | str | None = None
) -> Path:
    """Helper function to create crash test configuration.

    Args:
        restart_policy: Restart policy ("no", "always", "on-failure", "on-abnormal")
        crash_delay: Delay before crash in seconds (default: 0.5)
        exit_code: Exit code to use (default: 1)
        nats_host: NATS server host (default: "localhost")
        nats_port: NATS server port (default: 4222)
        restart_max: Maximum restarts (default: 0 = unlimited)
        temp: Create temporary file (default: True)
        output_path: Output file path (if temp=False)

    Returns:
        Path to generated configuration file
    """
    scenario = ServiceScenario(
        service_type="mock_crashing",
        instance_context=f"policy_{restart_policy}",
        config={
            "crash_delay": crash_delay,
            "exit_code": exit_code
        },
        restart=restart_policy,
        restart_max=restart_max,
        restart_sec=1.0,
        restart_window=60.0
    )

    generator = ConfigGenerator(nats_host=nats_host, nats_port=nats_port)
    return generator.generate_config([scenario], output_path=output_path, temp=temp)


def create_restart_limit_config(
    restart_policy: str,
    restart_max: int,
    restart_window: float,
    crash_delay: float = 0.5,
    nats_host: str = "localhost",
    nats_port: int = 4222,
    temp: bool = True,
    output_path: Path | str | None = None
) -> Path:
    """Helper function to create restart limit test configuration.

    Args:
        restart_policy: Restart policy ("always", "on-failure", "on-abnormal")
        restart_max: Maximum restarts in window
        restart_window: Time window for restart counting (seconds)
        crash_delay: Delay before crash in seconds (default: 0.5)
        nats_host: NATS server host (default: "localhost")
        nats_port: NATS server port (default: 4222)
        temp: Create temporary file (default: True)
        output_path: Output file path (if temp=False)

    Returns:
        Path to generated configuration file
    """
    scenario = ServiceScenario(
        service_type="mock_crashing",
        instance_context="restart_limit_test",
        config={
            "crash_delay": crash_delay,
            "exit_code": 1
        },
        restart=restart_policy,
        restart_max=restart_max,
        restart_sec=0.5,  # Fast restart for testing
        restart_window=restart_window
    )

    generator = ConfigGenerator(nats_host=nats_host, nats_port=nats_port)
    return generator.generate_config([scenario], output_path=output_path, temp=temp)
