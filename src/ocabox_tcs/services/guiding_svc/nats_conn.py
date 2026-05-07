"""NATS RPC + publishing for the guider service.

Subject scheme (configurable prefix; default ``svc``):

    <prefix>.rpc.<service>.<instance>.pipeline.<pipe>.v1.<cmd>
    <prefix>.publish.<service>.<instance>.pipeline.<pipe>.{state,events,journal}
    <prefix>.telemetry.<service>.<instance>.pipeline.<pipe>.correction
    <prefix>.telemetry.<service>.<instance>.active.correction

The ``<kind>`` segment (``rpc`` / ``publish`` / ``telemetry`` / ``heartbeat``)
is placed early so JetStream streams can be configured per kind.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from serverish.base import dt_utcnow_array
from serverish.messenger import (
    Messenger,
    get_journalpublisher,
    get_publisher,
    get_rpcresponder,
)
from serverish.messenger.msg_rpc_resp import MsgRpcResponder, Rpc

from ocabox_tcs.services.guiding_svc.state import Mode


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


RPC_COMMANDS = (
    "set_state",
    "set_mode",
    "acquire",
    "acquire_at",
    "snapshot",
    "dark_rebuild",
    "bias_rebuild",
    "manual_pulse",
)


class NatsConn:
    """RPC + publishing for one guider service instance.

    Args:
        manager: Back-reference to GuiderManager.
        subject_prefix: Root prefix (default ``svc``).
        service: Service short name (default ``guider``).
        instance: Instance identifier, e.g. ``jk15.guider_beso``.
    """

    def __init__(
        self,
        manager: Any,
        *,
        subject_prefix: str = "svc",
        service: str = "guider",
        instance: str,
    ) -> None:
        self.manager = manager
        self.svc_logger = manager.svc_logger
        self.subject_prefix = subject_prefix
        self.service = service
        self.instance = instance
        self._responders: list[MsgRpcResponder] = []

    @property
    def is_available(self) -> bool:
        try:
            return Messenger().is_open
        except Exception:  # noqa: BLE001
            return False

    async def open(self) -> None:
        if not self.is_available:
            self.svc_logger.warning(
                "NatsConn: Messenger not open — RPCs and publishing unavailable"
            )
        self.svc_logger.debug(
            "NatsConn opened (root=%s.<kind>.%s.%s)",
            self.subject_prefix, self.service, self.instance,
        )

    async def close(self) -> None:
        for resp in self._responders:
            try:
                await resp.close()
            except Exception as e:  # noqa: BLE001
                self.svc_logger.warning("NatsConn: responder close failed: %s", e)
        self._responders.clear()

    # ------------------------------------------------------------------
    # Subject builders
    # ------------------------------------------------------------------

    def _root(self, kind: str) -> str:
        return f"{self.subject_prefix}.{kind}.{self.service}.{self.instance}"

    def rpc_subject(self, pipe_id: str, cmd: str) -> str:
        return f"{self._root('rpc')}.pipeline.{pipe_id}.v1.{cmd}"

    def publish_subject(self, pipe_id: str, leaf: str) -> str:
        return f"{self._root('publish')}.pipeline.{pipe_id}.{leaf}"

    def telemetry_subject(self, pipe_id: str, leaf: str) -> str:
        return f"{self._root('telemetry')}.pipeline.{pipe_id}.{leaf}"

    # ------------------------------------------------------------------
    # RPC registration
    # ------------------------------------------------------------------

    async def register_pipeline_rpcs(
        self,
        cam_id: str,
        pipe_id: str,
        controller: Any,
    ) -> None:
        """Register the standard RPC suite for one pipeline.

        No-op (with a warning) when the Messenger is not open.
        """
        if not self.is_available:
            self.svc_logger.warning(
                "NatsConn: skipping RPC registration for %s (Messenger not open)",
                pipe_id,
            )
            return

        sender = f"{self.instance}.{pipe_id}"
        handlers: dict[str, Callable[[Rpc], Awaitable[None]]] = {
            "set_state": _wrap_handler(
                sender, lambda data: controller.set_state(data.get("patch", {}))
            ),
            "set_mode": _wrap_handler(
                sender, lambda data: controller.set_mode(data["mode"])
            ),
            "acquire": _wrap_handler(sender, lambda _data: controller.acquire()),
            "acquire_at": _wrap_handler(
                sender,
                lambda data: controller.acquire_at(x=data["x"], y=data["y"]),
            ),
            "snapshot": _wrap_handler(sender, lambda _data: controller.snapshot()),
            "dark_rebuild": _wrap_handler(
                sender, lambda data: controller.dark_rebuild(**(data or {}))
            ),
            "bias_rebuild": _wrap_handler(
                sender, lambda data: controller.bias_rebuild(**(data or {}))
            ),
            "manual_pulse": _wrap_handler(
                sender,
                lambda data: controller.manual_pulse(
                    direction=data["direction"],
                    duration_ms=data["duration_ms"],
                ),
            ),
        }

        for cmd, handler in handlers.items():
            subject = self.rpc_subject(pipe_id, cmd)
            responder = get_rpcresponder(subject=subject)
            await responder.register_function(callback=handler)
            self._responders.append(responder)
            self.svc_logger.debug("NatsConn: RPC %s registered", subject)

        self.svc_logger.info(
            "NatsConn: registered %d RPCs for pipeline %s.%s",
            len(handlers), self.instance, pipe_id,
        )

    # ------------------------------------------------------------------
    # Publishers (returned to Controller / Pipeline)
    # ------------------------------------------------------------------

    def state_publisher(self, cam_id: str, pipe_id: str) -> Any | None:
        if not self.is_available:
            return None
        return get_publisher(self.publish_subject(pipe_id, "state"))

    def correction_publisher(self, cam_id: str, pipe_id: str) -> Any | None:
        if not self.is_available:
            return None
        return get_publisher(self.telemetry_subject(pipe_id, "correction"))

    def camera_active_correction_publisher(self, cam_id: str) -> Any | None:
        """Per-camera active correction (decision #6)."""
        if not self.is_available:
            return None
        return get_publisher(f"{self._root('telemetry')}.active.correction")

    def thumbnail_notification_publisher(self, cam_id: str) -> Any | None:
        """Notifies that a new thumbnail is available on the NFS share.

        Payload references the file path; binary content is not sent over NATS.
        """
        if not self.is_available:
            return None
        return get_publisher(f"{self._root('publish')}.frame.thumbnail.ready")

    def journal_publisher(self, cam_id: str, pipe_id: str) -> Any | None:
        if not self.is_available:
            return None
        return get_journalpublisher(self.publish_subject(pipe_id, "journal"))

    def events_publisher(self, cam_id: str, pipe_id: str) -> Any | None:
        if not self.is_available:
            return None
        return get_publisher(self.publish_subject(pipe_id, "events"))


# ---------------------------------------------------------------------------
# Handler shaping
# ---------------------------------------------------------------------------


def _wrap_handler(
    sender: str,
    inner: Callable[[dict[str, Any]], Any],
) -> Callable[[Rpc], Awaitable[None]]:
    """Adapt a controller call into the (Rpc) -> None shape serverish wants.

    Shapes responses as ``{status, result, ts}`` on success or
    ``{status: error, error, detail, ts}`` on failure.
    """

    async def callback(rpc: Rpc) -> None:
        try:
            data = rpc.data or {}
            result = inner(data)
            if hasattr(result, "__await__"):
                result = await result
            payload = _serialise(result)
            await rpc.response_now(
                data={"status": "ok", "result": payload, "ts": dt_utcnow_array()},
                meta={"message_type": "rpc", "sender": sender},
            )
        except NotImplementedError as e:
            await rpc.response_now(
                data={
                    "status": "error",
                    "error": "not_implemented",
                    "detail": str(e),
                    "ts": dt_utcnow_array(),
                },
                meta={"message_type": "rpc", "sender": sender},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("RPC handler %s failed", sender)
            await rpc.response_now(
                data={
                    "status": "error",
                    "error": e.__class__.__name__,
                    "detail": str(e),
                    "ts": dt_utcnow_array(),
                },
                meta={"message_type": "rpc", "sender": sender},
            )

    return callback


def _serialise(value: Any) -> Any:
    """JSON-friendly conversion for RPC ``result`` payloads."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mode):
        return value.value
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    if hasattr(value, "to_dict"):
        return _serialise(value.to_dict())
    return repr(value)
