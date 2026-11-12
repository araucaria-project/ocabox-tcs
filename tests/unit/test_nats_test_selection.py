"""Regression coverage for external-service test selection."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.helpers.launcher_harness import ProcessHarness


ROOT = Path(__file__).resolve().parents[2]
NATS_MODULES = [
    "tests/unit/test_basic_lifecycle.py",
    "tests/unit/test_crash_scenarios.py",
    "tests/unit/test_event_verification.py",
    "tests/service_types/test_permanent_services.py",
    "tests/service_types/test_cyclic_services.py",
    "tests/service_types/test_single_shot_services.py",
]


@pytest.mark.parametrize("module", NATS_MODULES)
def test_nats_tests_are_deselected_by_default(module):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(ROOT / module)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, result.stdout + result.stderr
    assert "deselected" in result.stdout


def test_nats_marker_selects_all_integration_tests():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "nats",
            *(str(ROOT / module) for module in NATS_MODULES),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "deselected" not in result.stdout
    for module in NATS_MODULES:
        assert f"{module}::test_" in result.stdout


@pytest.mark.asyncio
async def test_launcher_uses_current_python_without_poetry(monkeypatch, tmp_path):
    popen = Mock(side_effect=OSError("stop before launch"))
    monkeypatch.setattr("tests.helpers.launcher_harness.subprocess.Popen", popen)
    config_path = tmp_path / "services.yaml"
    harness = ProcessHarness(config_path=config_path, nats_url="nats://localhost:4222")

    assert not await harness.start()
    assert popen.call_args.args[0] == [
        sys.executable,
        "-m",
        "ocabox_tcs.launchers.process",
        "--config",
        str(config_path),
    ]
