"""TicEndpoint — the service half of TIC's NATS service driver, against a fake messenger.

Pins the subjects, the on-change/keepalive status publishing, and the RpcReply contract for every
outcome a handler can have: result, refusal with its obcom code, unknown method, malformed request,
timeout, crash.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from datamodels.messages import RpcError, RpcOk, StatusValue

from ocabox_tcs import tic_endpoint as module
from ocabox_tcs.tic_endpoint import Call, Refusal, TicEndpoint


@pytest.fixture
def nats():
    publishers, responders = {}, {}

    def publisher(subject):
        return publishers.setdefault(subject, MagicMock(publish=AsyncMock()))

    def responder(subject):
        responders[subject] = MagicMock(register_function=AsyncMock(), close=AsyncMock())
        return responders[subject]

    with patch.object(module, 'get_publisher', side_effect=publisher), \
            patch.object(module, 'get_rpcresponder', side_effect=responder):
        yield SimpleNamespace(publishers=publishers, responders=responders)


async def _call(endpoint, subject, data):
    rpc = SimpleNamespace(nats_msg=SimpleNamespace(subject=subject), data=data, set_response=MagicMock())
    await endpoint._dispatch(rpc)
    reply = rpc.set_response.call_args.kwargs['data']
    return RpcOk.model_validate(reply) if reply['status'] == 'ok' else RpcError.model_validate(reply)


class TestStatus:

    async def test_publishes_on_change_only(self, nats):
        ep = TicEndpoint('optics', 'jk15')
        await ep.publish('camera', 'sees', 'dark')
        await ep.publish('camera', 'sees', 'dark')
        await ep.publish('camera', 'sees', 'sky.science')
        pub = nats.publishers['status.optics.jk15.camera.sees']
        assert [StatusValue.model_validate(c.kwargs['data']).value for c in pub.publish.await_args_list] == ['dark', 'sky.science']

    async def test_keepalive_repeats_with_the_original_ts(self, nats):
        ep = TicEndpoint('optics', 'jk15', keepalive=0.02)
        await ep.publish('self', 'operation', 'idle')
        first = nats.publishers['status.optics.jk15.self.operation'].publish.await_args.kwargs['data']
        await ep.start()
        await asyncio.sleep(0.07)
        await ep.stop()
        calls = nats.publishers['status.optics.jk15.self.operation'].publish.await_args_list
        assert len(calls) >= 3
        assert all(c.kwargs['data'] == first for c in calls)

    def test_tokens_are_validated(self):
        for realm, scope in (('optical-path', 'jk15'), ('optics', 'jk15.beso'), ('Optics', 'jk15')):
            with pytest.raises(ValueError):
                TicEndpoint(realm, scope)


class TestRpc:

    async def test_start_serves_the_scope_and_stop_closes(self, nats):
        ep = TicEndpoint('optics', 'jk15')
        await ep.start()
        responder = nats.responders['rpc.optics.jk15.>']
        responder.register_function.assert_awaited_once()
        await ep.stop()
        responder.close.assert_awaited_once()

    async def test_handler_gets_the_call_and_its_result_is_returned(self, nats):
        seen = []

        async def setpath(call: Call):
            seen.append(call)
            return {'accepted': True, 'moves': {'tertiary': 'andor'}}

        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', setpath)
        reply = await _call(ep, 'rpc.optics.jk15.camera.setpath',
                            {'parameters': {'function': 'object'}, 'caller': {'name': 'observer', 'id': 'ab'}, 'timeout': 5})
        assert reply == RpcOk(result={'accepted': True, 'moves': {'tertiary': 'andor'}})
        assert seen[0].element == 'camera' and seen[0].parameters == {'function': 'object'}
        assert seen[0].caller.name == 'observer' and seen[0].timeout == 5

    async def test_element_specific_handler_wins_over_any_element(self, nats):
        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', AsyncMock(return_value='any'))
        ep.method('setpath', AsyncMock(return_value='beso'), element='beso')
        assert (await _call(ep, 'rpc.optics.jk15.beso.setpath', {})).result == 'beso'
        assert (await _call(ep, 'rpc.optics.jk15.camera.setpath', {})).result == 'any'

    async def test_refusal_keeps_code_and_severity(self, nats):
        async def refuse(call):
            raise Refusal('covercalibrator: not enabled for automatic actuation')

        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', refuse)
        reply = await _call(ep, 'rpc.optics.jk15.camera.setpath', {})
        assert (reply.code, reply.severity.value) == (4009, 'NORMAL')
        assert 'not enabled' in reply.message

    async def test_unknown_method_is_1002_critical(self, nats):
        reply = await _call(TicEndpoint('optics', 'jk15'), 'rpc.optics.jk15.camera.explode', {})
        assert (reply.code, reply.severity.value) == (1002, 'CRITICAL')

    async def test_malformed_request_is_4001(self, nats):
        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', AsyncMock())
        assert (await _call(ep, 'rpc.optics.jk15.camera.setpath', {'timeout': -1})).code == 4001
        assert (await _call(ep, 'rpc.optics.jk15.camera', {})).code == 4001

    async def test_handler_slower_than_the_caller_waits_is_4002(self, nats):
        async def slow(call):
            await asyncio.sleep(1)

        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', slow)
        assert (await _call(ep, 'rpc.optics.jk15.camera.setpath', {'timeout': 0.05})).code == 4002

    async def test_handler_crash_is_2002_and_logged(self, nats, caplog):
        async def crash(call):
            raise RuntimeError('solver exploded')

        ep = TicEndpoint('optics', 'jk15')
        ep.method('setpath', crash)
        reply = await _call(ep, 'rpc.optics.jk15.camera.setpath', {})
        assert reply.code == 2002 and 'solver exploded' in reply.message
        assert 'camera.setpath failed' in caplog.text

    def test_for_service_uses_type_and_variant(self):
        svc = SimpleNamespace(svc_config=SimpleNamespace(type='optics', variant='jk15'), svc_logger=MagicMock())
        ep = TicEndpoint.for_service(svc)
        assert (ep.realm, ep.scope) == ('optics', 'jk15')
