"""Test CLI argument parsing and default behavior."""
import os
import subprocess
import sys
import time


# Get project root for PYTHONPATH
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_minimal_invocation_no_args():
    """Test running service with no arguments (uses all defaults)."""
    # Setup environment with PYTHONPATH
    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    # Run the external worker with no arguments
    process = subprocess.Popen(
        [sys.executable, "tests/external_example/external_worker.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Let it run briefly
    time.sleep(3)

    # Terminate
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    # Check output
    combined = stdout + stderr
    assert "TCS - Telescope Control Services" in combined
    assert "external_worker.dev" in combined  # Should use default variant "dev"
    assert "Config: Using defaults (no config file)" in combined
    assert "External worker tick" in combined


def test_minimal_invocation_context_only():
    """Test running service with only variant (no config file)."""
    # Setup environment with PYTHONPATH
    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    process = subprocess.Popen(
        [sys.executable, "tests/external_example/external_worker.py", "prod"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Let it run briefly
    time.sleep(3)

    # Terminate
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    # Check output
    combined = stdout + stderr
    assert "external_worker.prod" in combined  # Should use custom variant "prod"
    assert "Config: Using defaults (no config file)" in combined
    assert "External worker tick" in combined


def test_full_invocation_with_config():
    """Test running service with config file and variant (backward compatibility)."""
    # Setup environment with PYTHONPATH
    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    process = subprocess.Popen(
        [
            sys.executable,
            "tests/external_example/external_worker.py",
            "config/test_external.yaml",
            "demo"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Let it run briefly
    time.sleep(3)

    # Terminate
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    # Check output
    combined = stdout + stderr
    assert "external_worker.demo" in combined  # Should use specified variant "demo"
    assert "Config: config/test_external.yaml" in combined
    assert "External worker tick" in combined


def test_nats_default_connection_attempt():
    """Test that service attempts to connect to localhost:4222 by default."""
    # Setup environment with PYTHONPATH
    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    process = subprocess.Popen(
        [sys.executable, "tests/external_example/external_worker.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Let it run briefly
    time.sleep(3)

    # Terminate
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    # Check output for NATS connection attempt
    combined = stdout + stderr

    # Should either connect successfully or show connection attempt message
    assert (
        "Attempting NATS connection to localhost:4222" in combined or
        "Connected to NATS server" in combined or
        "Could not connect to NATS server" in combined or
        "Discovered existing open Messenger" in combined
    ), f"Expected NATS connection message not found in output:\n{combined}"


def test_parent_name_argument():
    """Test that --parent-name argument is accepted."""
    # Setup environment with PYTHONPATH
    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    process = subprocess.Popen(
        [
            sys.executable,
            "tests/external_example/external_worker.py",
            "dev",
            "--parent-name", "parent_service:main"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Let it run briefly
    time.sleep(3)

    # Terminate
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    # Check that service started successfully with parent-name arg
    combined = stdout + stderr
    assert "external_worker.dev" in combined
    assert "TCS - Telescope Control Services" in combined

    # Service should start successfully (parent_name doesn't cause errors)
    assert process.returncode in (0, -15)  # 0 = normal exit, -15 = SIGTERM
