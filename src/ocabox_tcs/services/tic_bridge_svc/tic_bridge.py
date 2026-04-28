"""TIC Bridge service.

Forwards NATS messages on ``tic.command.>`` (fire-and-forget) and ``tic.rpc.>``
(request-response) to the TIC server via ``ocabox``'s ``ClientAPI``. The subject
segments after the configured prefix are used verbatim as the TIC address;
payload fields (``parameters``, ``client_id``, ``request_timeout``, ``method``)
are passed through.

See :mod:`ocabox_tcs.services.tic_bridge_svc.bridge` for dispatch logic and
:mod:`ocabox_tcs.services.tic_bridge_svc.client_pool` for per-identity caching.
"""

from __future__ import annotations

from dataclasses import dataclass

from serverish.messenger import get_callbacksubscriber, get_rpcresponder

from ocabox_tcs.base_service import BasePermanentService, BaseServiceConfig, config, service
from ocabox_tcs.monitoring import Status
from ocabox_tcs.services.tic_bridge_svc.bridge import BridgeHandler
from ocabox_tcs.services.tic_bridge_svc.client_pool import ClientAPIPool


@config("tic_bridge_svc.tic_bridge")
@dataclass
class TicBridgeConfig(BaseServiceConfig):
    """Configuration for the TIC Bridge service."""

    # TIC endpoint — leave blank/zero to resolve via NATS config stream.
    tic_host: str = ""
    tic_port: int = 0
    obs_config_stream: str = "tic.config.observatory"
    config_load_timeout: float = 10.0

    # Default caller identity for requests without an explicit ``client_id``.
    # Blank → service id (``tic_bridge.<variant>``).
    default_client_name: str = ""
    default_user_email: str = ""
    default_user_description: str = "TIC Bridge Service"

    # Per-identity ClientAPI pool.
    max_clients: int = 20
    client_ttl: float = 3600.0

    # NATS subjects.
    command_prefix: str = "tic.command"
    rpc_prefix: str = "tic.rpc"
    enable_command: bool = True
    enable_rpc: bool = True

    # Seconds; converted to an absolute epoch deadline per request.
    default_request_timeout: float = 5.0


@service("tic_bridge_svc.tic_bridge")
class TicBridgeService(BasePermanentService):
    """Universal NATS ↔ TIC bridge."""

    def __init__(self) -> None:
        super().__init__()
        self._pool: ClientAPIPool | None = None
        self._handler: BridgeHandler | None = None
        self._command_sub = None  # MsgCallbackSubscriber
        self._rpc_resp = None  # MsgRpcResponder

    async def start_service(self) -> None:
        cfg: TicBridgeConfig = self.svc_config
        default_client_name = cfg.default_client_name or cfg.id

        self._pool = ClientAPIPool(
            logger=self.svc_logger,
            host=cfg.tic_host or None,
            port=cfg.tic_port or None,
            obs_config_stream=cfg.obs_config_stream,
            config_load_timeout=cfg.config_load_timeout,
            default_client_name=default_client_name,
            default_user_email=cfg.default_user_email,
            default_user_description=cfg.default_user_description,
            max_clients=cfg.max_clients,
            client_ttl=cfg.client_ttl,
        )
        await self._pool.initialize()

        self._handler = BridgeHandler(
            pool=self._pool,
            command_prefix=cfg.command_prefix,
            rpc_prefix=cfg.rpc_prefix,
            default_request_timeout=cfg.default_request_timeout,
            sender_id=cfg.id,
            logger=self.svc_logger,
        )

        if cfg.enable_command:
            # ``deliver_policy='new'``: on restart, skip any messages that arrived
            # while we were down. Fire-and-forget commands must not be replayed
            # after the fact (e.g. safety-cutoff engage is not idempotent in intent).
            self._command_sub = get_callbacksubscriber(
                f"{cfg.command_prefix}.>",
                deliver_policy="new",
            )
            await self._command_sub.open()
            await self._command_sub.subscribe(self._handler.handle_command)
            self.svc_logger.info("Subscribed to %s.> (commands)", cfg.command_prefix)

        if cfg.enable_rpc:
            self._rpc_resp = get_rpcresponder(subject=f"{cfg.rpc_prefix}.>")
            await self._rpc_resp.open()
            await self._rpc_resp.register_function(self._handler.handle_rpc)
            self.svc_logger.info("Registered RPC responder on %s.>", cfg.rpc_prefix)

        self.monitor.add_healthcheck_cb(self._healthcheck)
        self.svc_logger.info(
            "TIC Bridge ready (pool default=%s, max=%d)",
            default_client_name,
            cfg.max_clients,
        )

    async def stop_service(self) -> None:
        # Reverse order of start_service.
        if self._rpc_resp is not None:
            try:
                await self._rpc_resp.close()
            except Exception as e:  # pragma: no cover — defensive
                self.svc_logger.warning("Error closing RPC responder: %s", e)
            self._rpc_resp = None

        if self._command_sub is not None:
            try:
                await self._command_sub.close()
            except Exception as e:  # pragma: no cover — defensive
                self.svc_logger.warning("Error closing command subscriber: %s", e)
            self._command_sub = None

        if self._pool is not None:
            await self._pool.close()
            self._pool = None

        self._handler = None
        self.svc_logger.info("TIC Bridge stopped")

    def _healthcheck(self) -> Status | None:
        """Reflect recent-error rate; None = healthy."""
        if self._handler is None:
            return None
        if self._handler.recent_error_count >= BridgeHandler.ERROR_DEGRADED_THRESHOLD:
            return Status.DEGRADED
        return None


if __name__ == "__main__":
    TicBridgeService.main()
