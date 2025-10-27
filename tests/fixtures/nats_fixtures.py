"""NATS test server fixtures.

Provides pytest fixtures for running isolated NATS servers during tests.
Supports both subprocess-based and embedded NATS servers.
"""

import asyncio
import logging
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from nats.aio.client import Client as NATS

logger = logging.getLogger(__name__)


def find_free_port() -> int:
    """Find a free port on localhost.

    Returns:
        Available port number
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class NATSTestServer:
    """Manages a NATS server instance for testing.

    Starts NATS server as subprocess with JetStream enabled.
    Automatically creates required streams for ocabox-tcs.

    Args:
        port: NATS server port (default: auto-allocate)
        host: NATS server host (default: "localhost")
        debug: Enable NATS server debug logging
        jetstream_dir: Directory for JetStream storage (default: temp)
    """

    def __init__(
        self,
        port: int | None = None,
        host: str = "localhost",
        debug: bool = False,
        jetstream_dir: Path | None = None
    ):
        self.host = host
        self.port = port or find_free_port()
        self.debug = debug
        self.jetstream_dir = jetstream_dir
        self._process: subprocess.Popen | None = None
        self._nats_client: NATS | None = None

    @property
    def url(self) -> str:
        """Get NATS server URL."""
        return f"nats://{self.host}:{self.port}"

    async def start(self, timeout: float = 5.0) -> None:
        """Start NATS server and wait for readiness.

        Args:
            timeout: Max time to wait for server startup (seconds)

        Raises:
            RuntimeError: If server fails to start within timeout
        """
        logger.info(f"Starting NATS test server on {self.url}")

        # Build nats-server command
        cmd = [
            "nats-server",
            "-p", str(self.port),
            "-js",  # Enable JetStream
        ]

        if self.jetstream_dir:
            cmd.extend(["-sd", str(self.jetstream_dir)])

        if self.debug:
            cmd.extend(["-DV"])  # Debug + Verbose

        # Start server process
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE if not self.debug else None,
                stderr=subprocess.PIPE if not self.debug else None,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "nats-server not found. Install with: brew install nats-server"
            )

        # Wait for server to become ready
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                nc = NATS()
                await nc.connect(self.url)
                await nc.close()
                logger.info(f"NATS server ready at {self.url}")
                break
            except Exception:
                if self._process.poll() is not None:
                    raise RuntimeError("NATS server process terminated unexpectedly")
                await asyncio.sleep(0.1)
        else:
            await self.stop()
            raise RuntimeError(f"NATS server failed to start within {timeout}s")

        # Create required JetStream streams
        await self._create_streams()

    async def _create_streams(self) -> None:
        """Check for existing test stream.

        For tests, we use the existing 'test' stream which captures all test.> subjects.
        This stream should already exist in the NATS server setup.

        If the stream doesn't exist, we create it for convenience.
        """
        nc = NATS()
        try:
            await nc.connect(self.url)
            js = nc.jetstream()

            # Check if 'test' stream exists
            try:
                stream_info = await js.stream_info("test")
                logger.debug(f"Using existing 'test' stream (subjects: {stream_info.config.subjects})")
            except Exception:
                # Stream doesn't exist, create it
                logger.info("Creating 'test' stream for test infrastructure")
                try:
                    await js.add_stream(
                        name="test",
                        subjects=["test.>"],
                        retention="limits",
                        max_age=60 * 60,  # 1 hour
                        max_bytes=100 * 1024 * 1024,  # 100 MB
                        storage="file",
                    )
                    logger.info("Created 'test' stream")
                except Exception as e:
                    logger.warning(f"Failed to create 'test' stream: {e}")
        finally:
            await nc.close()

    async def stop(self) -> None:
        """Stop NATS server and cleanup."""
        if self._process:
            logger.info("Stopping NATS test server")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("NATS server did not terminate, killing")
                self._process.kill()
                self._process.wait()
            self._process = None

    async def get_client(self) -> NATS:
        """Get connected NATS client.

        Returns:
            Connected NATS client instance

        Note:
            Caller is responsible for closing the client
        """
        nc = NATS()
        await nc.connect(self.url)
        return nc

    async def __aenter__(self):
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.stop()
        return False


@pytest_asyncio.fixture
async def nats_server() -> AsyncGenerator[NATSTestServer, None]:
    """Pytest fixture providing NATS server connection.

    Universal fixture that:
    1. Tries to use existing NATS server on localhost:4222
    2. If not available, spawns a new NATS server on a free port
    3. Verifies 'test' stream exists and creates it if needed
    4. Purges test stream to ensure clean state for each test
    5. Cleans up only if we spawned the server ourselves

    Usage:
        async def test_something(nats_server):
            nc = await nats_server.get_client()
            # ... test code ...
            await nc.close()

    Yields:
        NATSTestServer instance
    """
    server = NATSTestServer(port=4222, host="localhost")
    spawned_server = False

    # Try to connect to existing server
    try:
        nc = NATS()
        await nc.connect(server.url, connect_timeout=2)
        await nc.close()
        logger.info(f"Using existing NATS server at {server.url}")

        # Verify/create test stream on existing server
        await server._create_streams()

    except Exception as e:
        # No existing server, spawn a new one
        logger.info(f"No existing NATS server found ({e}), spawning new server")
        server = NATSTestServer(port=find_free_port(), host="localhost")
        await server.start()
        spawned_server = True

    # Purge test stream to ensure clean state for each test
    try:
        nc = NATS()
        await nc.connect(server.url)
        js = nc.jetstream()
        await js.purge_stream("test")
        logger.debug("Purged 'test' stream for clean test state")
        await nc.close()
    except Exception as e:
        logger.warning(f"Failed to purge 'test' stream: {e}")

    try:
        yield server
    finally:
        # Only stop if we spawned the server
        if spawned_server:
            logger.info("Stopping spawned NATS server")
            await server.stop()
        else:
            logger.debug("Not stopping existing NATS server")


@pytest_asyncio.fixture
async def nats_client(nats_server: NATSTestServer) -> AsyncGenerator[NATS, None]:
    """Pytest fixture providing connected NATS client.

    Usage:
        async def test_something(nats_client):
            await nats_client.publish("test.subject", b"data")

    Yields:
        Connected NATS client instance
    """
    nc = await nats_server.get_client()
    try:
        yield nc
    finally:
        await nc.close()


@pytest.fixture
def nats_url(nats_server: NATSTestServer) -> str:
    """Pytest fixture providing NATS server URL.

    Usage:
        def test_something(nats_url):
            assert nats_url == "nats://localhost:4222"

    Returns:
        NATS server URL string
    """
    return nats_server.url
