"""TicEndpoint — the NATS side of a service that clients reach through TIC.

TIC serves the subtree ``<scope>.<realm>`` of such a service with its generic NATS protocol driver
(ocabox-server ``TreeNatsService``), so the service gets the ocabox client API, the error model and
TIC's access control without writing any of it. This class is the matching other half: it speaks
the subject grammar ``<class>.<realm>.<scope>.<element>.<item>`` (oca_nats_config#2) and the
``datamodels.messages`` contracts, and a service only writes its domain logic::

    endpoint = TicEndpoint.for_service(self)          # realm = service type, scope = variant
    endpoint.method('setpath', self.setpath)           # rpc.optics.jk15.<any element>.setpath
    await endpoint.start()
    await endpoint.publish('camera', 'sees', records)  # status.optics.jk15.camera.sees

Status is published on change and repeated every ``keepalive`` seconds (the value's ``ts`` stays
the time it was established). Liveness is the TCS heartbeat, not these repeats.

A method handler receives a :class:`Call` and returns the JSON result, or raises :class:`Refusal`
with an obcom error code (``4009`` by default) — the client gets exactly that code. Permissions
were checked by TIC before the request got here; ``Call.caller`` is for the log, never for access.
A long operation answers once it is accepted and reports progress as status, like a slew.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from datamodels.messages import Caller, RpcError, RpcOk, RpcRequest, Severity, StatusValue, Subject
from pydantic import JsonValue
from serverish.base.datetime import dt_to_array
from serverish.messenger import get_publisher, get_rpcresponder
from serverish.messenger.msg_rpc_resp import Rpc

if TYPE_CHECKING:
    from ocabox_tcs.base_service import BaseService

_log = logging.getLogger("tic_ep")


class Refusal(Exception):
    """Raised by a method handler to refuse a request; the client receives this obcom code."""

    def __init__(self, message: str, code: int = 4009, severity: Severity = Severity.NORMAL):
        super().__init__(message)
        self.message, self.code, self.severity = message, code, severity


@dataclass(frozen=True)
class Call:
    """One request to a method: ``rpc.<realm>.<scope>.<element>.<method>``."""

    element: str
    method: str
    parameters: dict[str, JsonValue]
    caller: Caller | None
    timeout: float | None  #: seconds the caller waits; answer well before


Handler = Callable[[Call], Awaitable[JsonValue]]


class TicEndpoint:
    """Status publisher and RPC responder of one service instance (``realm``) for one ``scope``.

    :param realm: the service's one-token TCS service type, e.g. ``optics``
    :param scope: the scope it serves, e.g. the telescope ``jk15`` (the TCS variant)
    :param keepalive: seconds between repeats of every published value
    """

    def __init__(self, realm: str, scope: str, keepalive: float = 60.0, logger: logging.Logger | None = None):
        Subject(message_class='status', realm=realm, scope=scope, element='self', item='x')  # validates the tokens
        self.realm, self.scope, self.keepalive = realm, scope, float(keepalive)
        self._log = logger or _log
        self._handlers: dict[tuple[str | None, str], Handler] = {}
        self._published: dict[tuple[str, str], StatusValue] = {}
        self._responder = None
        self._keepalive_task: asyncio.Task | None = None

    @classmethod
    def for_service(cls, service: BaseService, **kwargs) -> TicEndpoint:
        """Realm and scope from a TCS service: its type and its variant."""
        return cls(service.svc_config.type, service.svc_config.variant, logger=service.svc_logger, **kwargs)

    # ------------------------------------------------------------------ methods

    def method(self, name: str, handler: Handler, element: str | None = None) -> None:
        """Serve ``rpc.<realm>.<scope>.<element>.<name>`` with ``handler``; ``element=None`` serves the
        method on every element (the handler reads ``Call.element``)."""
        Subject(message_class='rpc', realm=self.realm, scope=self.scope, element=element or 'self', item=name)
        self._handlers[(element, name)] = handler

    async def _dispatch(self, rpc: Rpc) -> None:
        subject = rpc.nats_msg.subject
        reply = await self._answer(subject, rpc.data)
        rpc.set_response(data=reply.model_dump(mode='json'))

    async def _answer(self, subject: str, data: dict | None) -> RpcOk | RpcError:
        try:
            target = Subject.parse(subject)
            request = RpcRequest.model_validate(data or {})
        except ValueError as e:  # pydantic's ValidationError is a ValueError
            return RpcError(code=4001, severity=Severity.NORMAL, message=f'{subject}: malformed request: {e}')
        handler = self._handlers.get((target.element, target.item)) or self._handlers.get((None, target.item))
        if handler is None:
            return RpcError(code=1002, severity=Severity.CRITICAL,
                            message=f'no method {target.element}.{target.item}')
        call = Call(target.element, target.item, request.parameters, request.caller, request.timeout)
        who = request.caller.name if request.caller else 'unknown caller'
        self._log.info(f'{who}: {target.element}.{target.item}({request.parameters})')
        try:
            async with asyncio.timeout(call.timeout):
                return RpcOk(result=await handler(call))
        except Refusal as r:
            self._log.info(f'{who}: {target.element}.{target.item} refused ({r.code}): {r.message}')
            return RpcError(code=r.code, severity=r.severity, message=r.message)
        except TimeoutError:
            self._log.error(f'{target.element}.{target.item} did not answer within {call.timeout:.1f} s')
            return RpcError(code=4002, severity=Severity.NORMAL,
                            message=f'{target.element}.{target.item} did not answer in time')
        except Exception as e:
            self._log.exception(f'{target.element}.{target.item} failed')
            return RpcError(code=2002, severity=Severity.NORMAL, message=f'{target.element}.{target.item} failed: {e}')

    # ------------------------------------------------------------------ status

    async def publish(self, element: str, item: str, value: JsonValue, established: datetime | None = None) -> None:
        """Publish ``status.<realm>.<scope>.<element>.<item>`` if ``value`` changed. ``established`` is
        when the state came to be (default: now)."""
        key = (element, item)
        last = self._published.get(key)
        if last is not None and last.value == value:
            return
        status = StatusValue(value=value, ts=dt_to_array(established or datetime.now(timezone.utc)))
        self._published[key] = status
        await self._send(key, status)

    async def _send(self, key: tuple[str, str], status: StatusValue) -> None:
        subject = str(Subject(message_class='status', realm=self.realm, scope=self.scope, element=key[0], item=key[1]))
        await get_publisher(subject).publish(data=status.model_dump(mode='json'))

    async def _repeat(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive)
            for key, status in list(self._published.items()):
                try:
                    await self._send(key, status)
                except Exception:
                    self._log.exception(f'keepalive of {key[0]}.{key[1]} failed')

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._responder = get_rpcresponder(f'rpc.{self.realm}.{self.scope}.>')
        await self._responder.register_function(self._dispatch)
        self._keepalive_task = asyncio.create_task(self._repeat(), name=f'keepalive.{self.realm}.{self.scope}')
        self._log.info(f'serving rpc.{self.realm}.{self.scope}.> and publishing status.{self.realm}.{self.scope}.>')

    async def stop(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._responder is not None:
            await self._responder.close()
            self._responder = None
