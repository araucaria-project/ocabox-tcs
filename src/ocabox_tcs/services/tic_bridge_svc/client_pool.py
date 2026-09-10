"""ClientAPI pool for the TIC Bridge service.

A single underlying ZMQ ``Client`` is shared by all ``ClientAPI`` instances. Each
``ClientAPI`` carries its own ``TreeUser`` — built from the ``client_id`` carried
in an incoming NATS message — so TIC sees requests as coming from the original
caller's identity (important for access-grantor ``take_control``).

Identity is bound at ``ClientAPI`` construction time, not per-request, so a pool
is the only clean way to expose multiple identities through one bridge. See
``obcom.comunication.base_client_api.BaseClientAPI``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ob.comunication.client import Client
from ob.comunication.nats_cfg_loader import NatsCfgLoader
from ocaboxapi.client_api import ClientAPI


@dataclass
class PooledClient:
    """A cached ``ClientAPI`` along with its LRU bookkeeping."""

    api: ClientAPI
    created_at: float
    last_used: float


class ClientAPIPool:
    """Pool of ``ClientAPI`` instances keyed by ``client_id``.

    A single ``Client`` (ZMQ DEALER socket to TIC) is constructed in
    :meth:`initialize` and shared by every ``ClientAPI`` produced by the pool.
    The default entry (``client_id=None`` or ``""``) is created eagerly;
    per-``client_id`` entries are created lazily on first use and evicted with
    an LRU + TTL policy.
    """

    DEFAULT_KEY = ""  # internal key for the default (identity-less) ClientAPI

    def __init__(
        self,
        *,
        logger: logging.Logger,
        host: str | None = None,
        port: int | None = None,
        obs_config_stream: str = "tic.config.observatory",
        config_load_timeout: float = 10.0,
        default_client_name: str = "tic_bridge",
        default_user_email: str = "",
        default_user_description: str = "",
        max_clients: int = 20,
        client_ttl: float = 3600.0,
    ) -> None:
        self._logger = logger
        self._host = host
        self._port = port
        self._obs_config_stream = obs_config_stream
        self._config_load_timeout = config_load_timeout
        self._default_client_name = default_client_name
        self._default_user_email = default_user_email
        self._default_user_description = default_user_description
        self._max_clients = max_clients
        self._client_ttl = client_ttl

        self._client: Client | None = None
        self._pool: dict[str, PooledClient] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Resolve host/port, create the shared ``Client``, and eagerly create the default ``ClientAPI``."""
        if self._host is None or self._port is None:
            await self._load_tic_endpoint_from_nats()

        if self._host is None or self._port is None:
            raise RuntimeError(
                "TIC host/port unavailable: set tic_host/tic_port in service config "
                f"or ensure NATS stream {self._obs_config_stream!r} has client "
                f"{self._default_client_name!r} configured"
            )

        self._logger.info(
            "Connecting shared TIC Client name=%s host=%s port=%s",
            self._default_client_name,
            self._host,
            self._port,
        )
        self._client = Client(name=self._default_client_name, host=self._host, port=self._port)

        # Pre-create the default ClientAPI so the first call doesn't pay the construction cost.
        self._pool[self.DEFAULT_KEY] = self._build_pooled(
            name=self._default_client_name,
            user_name=self._default_client_name,
            user_email=self._default_user_email,
            user_description=self._default_user_description,
        )

    async def _load_tic_endpoint_from_nats(self) -> None:
        """Fill missing host/port from ``tic.config.observatory`` stream."""
        loader = NatsCfgLoader(stream=self._obs_config_stream)
        try:
            await loader.load_cfg(timeout=self._config_load_timeout)
        except TimeoutError:
            self._logger.warning(
                "Timeout loading TIC endpoint from NATS stream %r", self._obs_config_stream
            )
            return
        conn_cfg = loader.get_cfg_connection_client(self._default_client_name)
        if self._host is None:
            self._host = conn_cfg.get("url")
        if self._port is None:
            self._port = conn_cfg.get("port")

    async def get(self, client_id: str | None) -> ClientAPI:
        """Return a ``ClientAPI`` for ``client_id`` (default pool entry when falsy).

        Raises ``RuntimeError`` if the pool is closed or uninitialised.
        """
        if self._client is None:
            raise RuntimeError("ClientAPIPool.initialize() was not called")

        key = client_id if client_id else self.DEFAULT_KEY
        now = time.monotonic()

        async with self._lock:
            # Opportunistic TTL sweep (cheap: only touches non-default entries)
            self._evict_expired(now)

            entry = self._pool.get(key)
            if entry is not None:
                entry.last_used = now
                return entry.api

            # Making room before insert keeps pool at <= max_clients.
            self._evict_lru_if_full()

            # Empty client_id handled above via DEFAULT_KEY.
            entry = self._build_pooled(
                name=f"{self._default_client_name}[{key}]",
                user_name=key,
                user_email=self._default_user_email,
                user_description=self._default_user_description,
            )
            self._pool[key] = entry
            self._logger.debug(
                "Created ClientAPI for client_id=%r (pool size=%d)", key, len(self._pool)
            )
            return entry.api

    def _build_pooled(
        self,
        *,
        name: str,
        user_name: str,
        user_email: str,
        user_description: str,
    ) -> PooledClient:
        api = ClientAPI(
            name=name,
            client=self._client,
            user_name=user_name,
            user_email=user_email,
            user_description=user_description,
        )
        now = time.monotonic()
        return PooledClient(api=api, created_at=now, last_used=now)

    def _evict_expired(self, now: float) -> None:
        """Drop non-default entries idle for longer than ``client_ttl``."""
        if self._client_ttl <= 0:
            return
        stale = [
            key
            for key, entry in self._pool.items()
            if key != self.DEFAULT_KEY and (now - entry.last_used) > self._client_ttl
        ]
        for key in stale:
            self._pool.pop(key, None)
            self._logger.debug("Evicted idle ClientAPI for client_id=%r", key)

    def _evict_lru_if_full(self) -> None:
        """Drop the least-recently-used non-default entry if at capacity."""
        if len(self._pool) < self._max_clients:
            return
        victim = min(
            (key for key in self._pool if key != self.DEFAULT_KEY),
            key=lambda k: self._pool[k].last_used,
            default=None,
        )
        if victim is not None:
            self._pool.pop(victim, None)
            self._logger.debug("LRU-evicted ClientAPI for client_id=%r", victim)

    async def close(self) -> None:
        """Drop all pooled ``ClientAPI`` references.

        The shared ZMQ socket is released by ``Client.__del__`` once no other
        references remain; there is no explicit ``Client.close()`` to call.
        """
        async with self._lock:
            self._pool.clear()
            self._client = None

    @property
    def size(self) -> int:
        return len(self._pool)
