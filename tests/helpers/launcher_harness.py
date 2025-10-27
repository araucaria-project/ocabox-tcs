"""Launcher test harnesses.

Provides unified interface for testing different launcher types.
Each harness abstracts launcher-specific start/stop/control mechanisms.
"""

import asyncio
import logging
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServiceScenario:
    """Declarative service scenario definition.

    Describes a service to be launched and its expected behavior.

    Args:
        service_type: Service type name (e.g., "mock_permanent", "mock_crashing")
        instance_context: Instance context identifier
        config: Service-specific configuration dict
        restart: Restart policy ("no", "always", "on-failure", "on-abnormal")
        restart_sec: Delay before restart (seconds)
        restart_max: Maximum restarts in window (0 = unlimited)
        restart_window: Time window for restart counting (seconds)
        expected_status: Expected final status (e.g., "ok", "failed", "shutdown")
        expected_lifecycle: Expected lifecycle events (e.g., ["start", "crash", "restarting"])
        timeout: Max time to wait for expected state (seconds)
    """
    service_type: str
    instance_context: str
    config: dict[str, Any] | None = None
    restart: str = "no"
    restart_sec: float = 1.0
    restart_max: int = 0
    restart_window: float = 60.0
    expected_status: str | None = None
    expected_lifecycle: list[str] | None = None
    timeout: float = 10.0


class LauncherHarness(ABC):
    """Base class for launcher test harnesses.

    Provides unified interface for testing different launcher types.
    Subclasses implement launcher-specific mechanisms.

    Args:
        config_path: Path to launcher configuration file
        nats_url: NATS server URL for monitoring
        launcher_id: Optional custom launcher ID
    """

    def __init__(
        self,
        config_path: Path | str,
        nats_url: str,
        launcher_id: str | None = None
    ):
        self.config_path = Path(config_path)
        self.nats_url = nats_url
        self.launcher_id = launcher_id
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Check if launcher is currently running."""
        return self._is_running

    @abstractmethod
    async def start(self, timeout: float = 10.0) -> bool:
        """Start launcher and wait for readiness.

        Args:
            timeout: Max time to wait for startup (seconds)

        Returns:
            True if launcher started successfully, False otherwise
        """
        pass

    @abstractmethod
    async def stop(self, timeout: float = 10.0) -> bool:
        """Stop launcher and wait for shutdown.

        Args:
            timeout: Max time to wait for shutdown (seconds)

        Returns:
            True if launcher stopped successfully, False otherwise
        """
        pass

    @abstractmethod
    async def restart(self, timeout: float = 10.0) -> bool:
        """Restart launcher.

        Args:
            timeout: Max time to wait for restart (seconds)

        Returns:
            True if launcher restarted successfully, False otherwise
        """
        pass

    @abstractmethod
    async def get_service_status(self, service_id: str) -> dict[str, Any]:
        """Get status of specific service.

        Args:
            service_id: Service identifier (service_type:instance_context)

        Returns:
            Service status dictionary
        """
        pass

    @abstractmethod
    async def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all services.

        Returns:
            Dictionary mapping service IDs to status dicts
        """
        pass

    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.stop()
        return False


class ProcessHarness(LauncherHarness):
    """Harness for process-based launcher testing.

    Manages launcher subprocess with proper lifecycle control.
    Monitors launcher output and provides status access.
    """

    def __init__(
        self,
        config_path: Path | str,
        nats_url: str,
        launcher_id: str | None = None,
        capture_output: bool = True
    ):
        super().__init__(config_path, nats_url, launcher_id)
        self.capture_output = capture_output
        self._process: subprocess.Popen | None = None
        self._output_lines: list[str] = []
        self._output_task: asyncio.Task | None = None

    async def start(self, timeout: float = 10.0) -> bool:
        """Start process launcher subprocess.

        Args:
            timeout: Max time to wait for startup (seconds)

        Returns:
            True if launcher started successfully, False otherwise
        """
        if self._is_running:
            logger.warning("Launcher already running")
            return False

        logger.info(f"Starting process launcher: {self.config_path}")

        # Build command
        cmd = [
            "poetry", "run", "tcs_process",
            "--config", str(self.config_path)
        ]

        # Add environment variables
        env = {
            "NATS_HOST": self.nats_url.split("://")[1].split(":")[0],
            "NATS_PORT": self.nats_url.split(":")[-1],
        }

        # Start launcher process
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE if self.capture_output else None,
                stderr=subprocess.STDOUT if self.capture_output else None,
                env={**subprocess.os.environ, **env},
                text=True,
            )
        except Exception as e:
            logger.error(f"Failed to start launcher: {e}")
            return False

        # Start output capture task
        if self.capture_output:
            self._output_task = asyncio.create_task(self._capture_output())

        # Wait for launcher to become ready
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            # Check if process is still running
            if self._process.poll() is not None:
                logger.error("Launcher process terminated unexpectedly")
                return False

            # Check for readiness indicators in output
            if self.capture_output and self._output_lines:
                for line in self._output_lines:
                    if "Services started" in line or "Press Ctrl+C to stop" in line:
                        self._is_running = True
                        logger.info("Process launcher ready")
                        return True

            await asyncio.sleep(0.1)

        logger.warning(f"Launcher did not become ready within {timeout}s")
        return False

    async def stop(self, timeout: float = 10.0) -> bool:
        """Stop process launcher subprocess.

        Args:
            timeout: Max time to wait for shutdown (seconds)

        Returns:
            True if launcher stopped successfully, False otherwise
        """
        if not self._is_running or self._process is None:
            logger.warning("Launcher not running")
            return False

        logger.info("Stopping process launcher")

        # Send SIGTERM
        self._process.send_signal(signal.SIGTERM)

        # Wait for graceful shutdown
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, self._process.wait
                ),
                timeout=timeout
            )
            logger.info("Process launcher stopped gracefully")
        except asyncio.TimeoutError:
            logger.warning("Launcher did not stop gracefully, killing")
            self._process.kill()
            await asyncio.get_event_loop().run_in_executor(
                None, self._process.wait
            )

        # Cancel output capture task
        if self._output_task:
            self._output_task.cancel()
            try:
                await self._output_task
            except asyncio.CancelledError:
                pass

        self._is_running = False
        self._process = None
        return True

    async def restart(self, timeout: float = 10.0) -> bool:
        """Restart process launcher.

        Args:
            timeout: Max time to wait for restart (seconds)

        Returns:
            True if launcher restarted successfully, False otherwise
        """
        await self.stop(timeout / 2)
        return await self.start(timeout / 2)

    async def get_service_status(self, service_id: str) -> dict[str, Any]:
        """Get status of specific service via NATS monitoring.

        Args:
            service_id: Service identifier (service_type:instance_context)

        Returns:
            Service status dictionary with keys:
                - service_id: Service identifier
                - status: Current status (ok/error/failed/shutdown/unknown)
                - message: Status message
                - timestamp: Last update timestamp
        """
        from nats.aio.client import Client as NATS

        nc = NATS()
        try:
            await nc.connect(self.nats_url)
            js = nc.jetstream()

            # Query latest status from test stream (test.svc.status subjects)
            try:
                psub = await js.pull_subscribe(
                    f"test.svc.status.{service_id}",
                    durable=None,
                    stream="test"
                )
                msgs = await psub.fetch(batch=1, timeout=1.0)
                if msgs:
                    import json
                    payload = json.loads(msgs[0].data.decode())
                    data = payload.get("data", {})
                    return {
                        "service_id": service_id,
                        "status": data.get("status", "unknown"),
                        "message": data.get("message", ""),
                        "timestamp": data.get("timestamp", [])
                    }
                await psub.unsubscribe()
            except Exception:
                pass  # No status messages yet

            # Fallback: check registry events in test stream
            try:
                psub = await js.pull_subscribe(
                    f"test.svc.registry.*.{service_id}",
                    durable=None,
                    stream="test"
                )
                msgs = await psub.fetch(batch=1, timeout=1.0)
                if msgs:
                    import json
                    payload = json.loads(msgs[0].data.decode())
                    data = payload.get("data", {})
                    return {
                        "service_id": service_id,
                        "status": data.get("status", "unknown"),
                        "message": data.get("event", ""),
                        "timestamp": data.get("timestamp", [])
                    }
                await psub.unsubscribe()
            except Exception:
                pass

            return {"service_id": service_id, "status": "unknown", "message": "", "timestamp": []}

        finally:
            await nc.close()

    async def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all services via NATS monitoring.

        Returns:
            Dictionary mapping service IDs to status dicts
        """
        from nats.aio.client import Client as NATS
        import json

        nc = NATS()
        statuses = {}

        try:
            await nc.connect(self.nats_url)
            js = nc.jetstream()

            # Query all status messages from test stream
            try:
                psub = await js.pull_subscribe(
                    "test.svc.status.>",
                    durable=None,
                    stream="test"
                )
                msgs = await psub.fetch(batch=100, timeout=1.0)
                for msg in msgs:
                    payload = json.loads(msg.data.decode())
                    data = payload.get("data", {})
                    service_id = data.get("service_id", "")
                    if service_id:
                        statuses[service_id] = {
                            "service_id": service_id,
                            "status": data.get("status", "unknown"),
                            "message": data.get("message", ""),
                            "timestamp": data.get("timestamp", [])
                        }
                await psub.unsubscribe()
            except Exception as e:
                logger.debug(f"Error querying status: {e}")

            # Fallback: get service list from registry in test stream
            if not statuses:
                try:
                    psub = await js.pull_subscribe(
                        "test.svc.registry.>",
                        durable=None,
                        stream="test"
                    )
                    msgs = await psub.fetch(batch=100, timeout=1.0)
                    for msg in msgs:
                        payload = json.loads(msg.data.decode())
                        data = payload.get("data", {})
                        service_id = data.get("service_id", "")
                        if service_id and service_id not in statuses:
                            statuses[service_id] = {
                                "service_id": service_id,
                                "status": data.get("status", "unknown"),
                                "message": data.get("event", ""),
                                "timestamp": data.get("timestamp", [])
                            }
                    await psub.unsubscribe()
                except Exception as e:
                    logger.debug(f"Error querying registry: {e}")

        finally:
            await nc.close()

        return statuses

    async def _capture_output(self) -> None:
        """Background task to capture launcher output."""
        if not self._process or not self._process.stdout:
            return

        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(
                    None, self._process.stdout.readline
                )
                if not line:
                    break
                line = line.strip()
                if line:
                    self._output_lines.append(line)
                    logger.debug(f"Launcher: {line}")
            except Exception as e:
                logger.error(f"Error capturing output: {e}")
                break

    def get_output(self) -> list[str]:
        """Get captured launcher output.

        Returns:
            List of output lines
        """
        return self._output_lines.copy()

    def get_last_lines(self, n: int = 10) -> list[str]:
        """Get last N lines of launcher output.

        Args:
            n: Number of lines to return

        Returns:
            List of last N output lines
        """
        return self._output_lines[-n:]


class AsyncioHarness(LauncherHarness):
    """Harness for asyncio-based launcher testing.

    Manages launcher in same process with separate event loop.
    Provides direct access to launcher state.
    """

    def __init__(
        self,
        config_path: Path | str,
        nats_url: str,
        launcher_id: str | None = None
    ):
        super().__init__(config_path, nats_url, launcher_id)
        self._launcher = None
        self._launcher_task: asyncio.Task | None = None

    async def start(self, timeout: float = 10.0) -> bool:
        """Start asyncio launcher in same process.

        Args:
            timeout: Max time to wait for startup (seconds)

        Returns:
            True if launcher started successfully, False otherwise
        """
        if self._is_running:
            logger.warning("Launcher already running")
            return False

        logger.info(f"Starting asyncio launcher: {self.config_path}")

        try:
            # Import launcher (deferred to avoid import side effects)
            from ocabox_tcs.launchers.asyncio import AsyncioLauncher

            # Create launcher instance
            self._launcher = AsyncioLauncher()

            # Initialize with config
            # TODO: Load config from file and pass to launcher
            # For now, this is a placeholder
            success = await self._launcher.initialize({})
            if not success:
                logger.error("Failed to initialize launcher")
                return False

            # Start all services
            success = await self._launcher.start_all()
            if not success:
                logger.error("Failed to start services")
                return False

            self._is_running = True
            logger.info("Asyncio launcher ready")
            return True

        except Exception as e:
            logger.error(f"Failed to start launcher: {e}")
            return False

    async def stop(self, timeout: float = 10.0) -> bool:
        """Stop asyncio launcher.

        Args:
            timeout: Max time to wait for shutdown (seconds)

        Returns:
            True if launcher stopped successfully, False otherwise
        """
        if not self._is_running or self._launcher is None:
            logger.warning("Launcher not running")
            return False

        logger.info("Stopping asyncio launcher")

        try:
            await asyncio.wait_for(
                self._launcher.stop_all(),
                timeout=timeout
            )
            logger.info("Asyncio launcher stopped")
        except asyncio.TimeoutError:
            logger.warning("Launcher did not stop within timeout")
            return False
        except Exception as e:
            logger.error(f"Error stopping launcher: {e}")
            return False

        self._is_running = False
        self._launcher = None
        return True

    async def restart(self, timeout: float = 10.0) -> bool:
        """Restart asyncio launcher.

        Args:
            timeout: Max time to wait for restart (seconds)

        Returns:
            True if launcher restarted successfully, False otherwise
        """
        await self.stop(timeout / 2)
        return await self.start(timeout / 2)

    async def get_service_status(self, service_id: str) -> dict[str, Any]:
        """Get status of specific service directly from launcher.

        Args:
            service_id: Service identifier (service_type:instance_context)

        Returns:
            Service status dictionary
        """
        if not self._launcher:
            return {"service_id": service_id, "status": "unknown"}

        all_status = await self._launcher.get_status()
        return all_status.get(service_id, {"service_id": service_id, "status": "unknown"})

    async def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all services directly from launcher.

        Returns:
            Dictionary mapping service IDs to status dicts
        """
        if not self._launcher:
            return {}

        return await self._launcher.get_status()
