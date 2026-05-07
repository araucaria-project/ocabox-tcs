"""Request dispatcher for the TIC Bridge service.

Translates a NATS message on ``<prefix>.<tic-address>`` into a TIC PUT/GET via
the appropriate pooled ``ClientAPI`` and, for RPC, shapes the response.

The bridge trusts the caller: no validation of telescope id or command name.
Subject segments after the configured prefix *are* the TIC address, forwarded
verbatim.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from obcom.comunication.comunication_error import (
    CommunicationRuntimeError,
    CommunicationTimeoutError,
)
from obcom.data_colection.value_call import ValueResponse
from serverish.base import dt_utcnow_array
from serverish.messenger.msg_rpc_resp import Rpc

from ocabox_tcs.services.tic_bridge_svc.client_pool import ClientAPIPool


# TIC error codes (see ocabox-server tree_base_request_blocker; also PR #5 for 1005)
TIC_ERR_ACCESS_DENIED = 1004
TIC_ERR_SAFETY_CUTOFF = 1005


@dataclass
class _BridgeRequest:
    """Parsed view of an incoming NATS message."""

    address: str
    parameters: dict
    client_id: str | None
    request_timeout: float | None  # seconds, relative
    method: str  # "GET" or "PUT"


def extract_tic_address(subject: str, prefix: str) -> str:
    """Strip ``prefix.`` from ``subject`` and return the remainder.

    ``tic.command.T1.access_grantor.engage_safety_cutoff`` with prefix
    ``tic.command`` yields ``T1.access_grantor.engage_safety_cutoff``.
    """
    if not subject.startswith(prefix + "."):
        raise ValueError(f"subject {subject!r} does not start with {prefix!r}")
    tail = subject[len(prefix) + 1 :]
    if not tail:
        raise ValueError(f"subject {subject!r} has no TIC address after prefix")
    return tail


class BridgeHandler:
    """Dispatches NATS messages onto the TIC ``ClientAPI`` pool."""

    # Rolling window for the "recent errors" healthcheck signal.
    ERROR_WINDOW_SECONDS = 60.0
    ERROR_DEGRADED_THRESHOLD = 5

    def __init__(
        self,
        *,
        pool: ClientAPIPool,
        command_prefix: str,
        rpc_prefix: str,
        default_request_timeout: float,
        sender_id: str,
        logger: logging.Logger,
    ) -> None:
        self._pool = pool
        self._command_prefix = command_prefix
        self._rpc_prefix = rpc_prefix
        self._default_request_timeout = default_request_timeout
        self._sender_id = sender_id
        self._logger = logger
        self._recent_errors: deque[float] = deque()

    # ------------------------------------------------------------------ helpers

    def _parse(
        self, subject: str, prefix: str, data: dict | None, default_method: str
    ) -> _BridgeRequest:
        data = data or {}
        return _BridgeRequest(
            address=extract_tic_address(subject, prefix),
            parameters=dict(data.get("parameters") or {}),
            client_id=data.get("client_id") or None,
            request_timeout=_coerce_positive_float(data.get("request_timeout")),
            method=str(data.get("method") or default_method).upper(),
        )

    def _absolute_timeout(self, relative: float | None) -> float:
        return time.time() + (relative if relative is not None else self._default_request_timeout)

    def _record_error(self) -> None:
        now = time.monotonic()
        self._recent_errors.append(now)
        cutoff = now - self.ERROR_WINDOW_SECONDS
        while self._recent_errors and self._recent_errors[0] < cutoff:
            self._recent_errors.popleft()

    @property
    def recent_error_count(self) -> int:
        cutoff = time.monotonic() - self.ERROR_WINDOW_SECONDS
        while self._recent_errors and self._recent_errors[0] < cutoff:
            self._recent_errors.popleft()
        return len(self._recent_errors)

    # ---------------------------------------------------------------- command

    async def handle_command(self, data: dict, meta: dict) -> bool:
        """Callback for ``MsgCallbackSubscriber`` on ``tic.command.>``.

        Returns ``True`` so the subscriber keeps reading. Errors are logged but
        never raised — this is a fire-and-forget channel.
        """
        subject = (meta or {}).get("nats", {}).get("subject")
        if not subject:
            self._logger.error("Command message without nats.subject metadata; dropping")
            self._record_error()
            return True

        try:
            req = self._parse(subject, self._command_prefix, data, default_method="PUT")
        except ValueError as e:
            self._logger.error("Malformed command subject %r: %s", subject, e)
            self._record_error()
            return True

        try:
            api = await self._pool.get(req.client_id)
        except Exception as e:
            self._logger.error("Failed to obtain ClientAPI for client_id=%r: %s", req.client_id, e)
            self._record_error()
            return True

        try:
            # no_wait=False so we can surface errors; caller doesn't see them.
            response: ValueResponse | None = await api.put_async(
                req.address,
                parameters_dict=req.parameters,
                request_timeout=self._absolute_timeout(req.request_timeout),
                no_wait=False,
            )
        except CommunicationTimeoutError:
            self._logger.warning(
                "TIC timeout on command %s (client_id=%r)", req.address, req.client_id
            )
            self._record_error()
            return True
        except CommunicationRuntimeError as e:
            self._logger.warning(
                "TIC comm error on command %s (client_id=%r): %s", req.address, req.client_id, e
            )
            self._record_error()
            return True
        except Exception as e:  # pragma: no cover — defensive
            self._logger.exception("Unexpected error on command %s: %s", req.address, e)
            self._record_error()
            return True

        if response is not None and not response.status:
            code = getattr(response.error, "code", None)
            message = getattr(response.error, "message", None)
            self._logger.warning(
                "TIC rejected command %s (client_id=%r, code=%s): %s",
                req.address,
                req.client_id,
                code,
                message,
            )
            self._record_error()
        else:
            self._logger.debug("Command %s dispatched (client_id=%r)", req.address, req.client_id)
        return True

    # -------------------------------------------------------------------- rpc

    async def handle_rpc(self, rpc: Rpc) -> None:
        """Callback for ``MsgRpcResponder`` on ``tic.rpc.>``."""
        subject = rpc.nats_msg.subject if rpc.nats_msg is not None else None
        if not subject:
            rpc.set_response(
                data=self._error_payload("invalid_request", "missing subject"),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        try:
            req = self._parse(subject, self._rpc_prefix, rpc.data, default_method="GET")
        except ValueError as e:
            rpc.set_response(
                data=self._error_payload("invalid_request", str(e)),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        if req.method not in ("GET", "PUT"):
            rpc.set_response(
                data=self._error_payload("invalid_request", f"unsupported method {req.method!r}"),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        try:
            api = await self._pool.get(req.client_id)
        except Exception as e:
            rpc.set_response(
                data=self._error_payload("pool_exhausted", str(e)),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        try:
            if req.method == "GET":
                response = await api.get_async(
                    req.address,
                    parameters_dict=req.parameters,
                    request_timeout=self._absolute_timeout(req.request_timeout),
                )
            else:
                response = await api.put_async(
                    req.address,
                    parameters_dict=req.parameters,
                    request_timeout=self._absolute_timeout(req.request_timeout),
                    no_wait=False,
                )
        except CommunicationTimeoutError:
            rpc.set_response(data=self._error_payload("timeout", None), meta=self._response_meta())
            self._record_error()
            return
        except CommunicationRuntimeError as e:
            rpc.set_response(
                data=self._error_payload("comm_error", str(e)), meta=self._response_meta()
            )
            self._record_error()
            return
        except Exception as e:  # pragma: no cover — defensive
            self._logger.exception("Unexpected error on RPC %s: %s", req.address, e)
            rpc.set_response(
                data=self._error_payload("internal_error", str(e)), meta=self._response_meta()
            )
            self._record_error()
            return

        if response is None:
            rpc.set_response(
                data=self._error_payload("no_response", None),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        if not response.status:
            code = getattr(response.error, "code", None)
            message = getattr(response.error, "message", None)
            error_kind = _classify_tic_error(code)
            rpc.set_response(
                data=self._error_payload(error_kind, message, code=code),
                meta=self._response_meta(),
            )
            self._record_error()
            return

        result = response.value.v if response.value is not None else None
        rpc.set_response(
            data={"status": "ok", "result": result, "ts": dt_utcnow_array()},
            meta=self._response_meta(),
        )

    # ---------------------------------------------------------------- shaping

    def _response_meta(self) -> dict:
        return {"message_type": "rpc", "sender": self._sender_id}

    @staticmethod
    def _error_payload(kind: str, message: Any, *, code: int | None = None) -> dict:
        payload: dict = {"status": "error", "error": kind, "ts": dt_utcnow_array()}
        if message is not None:
            payload["message"] = str(message)
        if code is not None:
            payload["code"] = code
        return payload


def _classify_tic_error(code: int | None) -> str:
    if code == TIC_ERR_ACCESS_DENIED:
        return "access_denied"
    if code == TIC_ERR_SAFETY_CUTOFF:
        return "safety_cutoff"
    return "server_error"


def _coerce_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
