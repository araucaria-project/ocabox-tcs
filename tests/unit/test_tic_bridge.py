"""Unit tests for the TIC Bridge service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from obcom.comunication.comunication_error import (
    CommunicationRuntimeError,
    CommunicationTimeoutError,
)

from ocabox_tcs.services.tic_bridge_svc.bridge import (
    BridgeHandler,
    extract_tic_address,
)
from ocabox_tcs.services.tic_bridge_svc.client_pool import ClientAPIPool
from ocabox_tcs.services.tic_bridge_svc.tic_bridge import (
    TicBridgeConfig,
    TicBridgeService,
)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def logger() -> logging.Logger:
    lg = logging.getLogger("tic_bridge_test")
    lg.addHandler(logging.NullHandler())
    return lg


def _response(
    status: bool = True, value_v=None, err_code: int | None = None, err_msg: str | None = None
):
    """Shape a fake ValueResponse — matches what ClientAPI returns."""
    err = None
    if err_code is not None or err_msg is not None:
        err = SimpleNamespace(code=err_code, message=err_msg)
    value = SimpleNamespace(v=value_v) if value_v is not None or status else None
    return SimpleNamespace(status=status, value=value, error=err)


# ----------------------------------------------------------------- config tests


class TestTicBridgeConfig:
    def test_defaults(self):
        cfg = TicBridgeConfig(type="tic_bridge_svc.tic_bridge", variant="main")
        assert cfg.tic_host == ""
        assert cfg.tic_port == 0
        assert cfg.obs_config_stream == "tic.config.observatory"
        assert cfg.default_client_name == ""
        assert cfg.max_clients == 20
        assert cfg.client_ttl == 3600.0
        assert cfg.command_prefix == "tic.command"
        assert cfg.rpc_prefix == "tic.rpc"
        assert cfg.enable_command is True
        assert cfg.enable_rpc is True
        assert cfg.default_request_timeout == 5.0

    def test_id_format(self):
        cfg = TicBridgeConfig(type="tic_bridge_svc.tic_bridge", variant="main")
        assert cfg.id == "tic_bridge_svc.tic_bridge.main"

    def test_custom(self):
        cfg = TicBridgeConfig(
            type="tic_bridge_svc.tic_bridge",
            variant="dev",
            tic_host="tic.example.com",
            tic_port=5559,
            max_clients=5,
            command_prefix="custom.cmd",
        )
        assert cfg.tic_host == "tic.example.com"
        assert cfg.tic_port == 5559
        assert cfg.max_clients == 5
        assert cfg.command_prefix == "custom.cmd"


# --------------------------------------------------------------- subject parsing


class TestSubjectParsing:
    def test_command_prefix(self):
        assert (
            extract_tic_address("tic.command.T1.access_grantor.engage_safety_cutoff", "tic.command")
            == "T1.access_grantor.engage_safety_cutoff"
        )

    def test_rpc_prefix(self):
        assert (
            extract_tic_address("tic.rpc.T1.mount.rightascension", "tic.rpc")
            == "T1.mount.rightascension"
        )

    def test_deep_address(self):
        assert (
            extract_tic_address("tic.command.T1.sub.sub2.cmd", "tic.command") == "T1.sub.sub2.cmd"
        )

    def test_custom_prefix(self):
        assert extract_tic_address("foo.bar.X.y", "foo.bar") == "X.y"

    def test_mismatched_prefix(self):
        with pytest.raises(ValueError):
            extract_tic_address("other.T1.x", "tic.command")

    def test_empty_tail(self):
        with pytest.raises(ValueError):
            extract_tic_address("tic.command", "tic.command")

    def test_prefix_without_trailing_dot(self):
        # "tic.commandX.y" must not be treated as having prefix "tic.command"
        with pytest.raises(ValueError):
            extract_tic_address("tic.commandX.y", "tic.command")


# ---------------------------------------------------------------- pool tests


class TestClientAPIPool:
    @pytest.fixture
    def fake_client_cls(self, monkeypatch):
        """Replace ``Client`` in client_pool with a stub (ZMQ socket side-effect avoided)."""
        fake = MagicMock(name="ClientStub")
        monkeypatch.setattr("ocabox_tcs.services.tic_bridge_svc.client_pool.Client", fake)
        return fake

    @pytest.fixture
    def fake_clientapi_cls(self, monkeypatch):
        """Replace ``ClientAPI`` with a factory that records per-call kwargs."""
        calls = []

        def factory(**kwargs):
            instance = MagicMock(name="ClientAPI")
            instance.put_async = AsyncMock()
            instance.get_async = AsyncMock()
            instance.kwargs = kwargs
            calls.append(kwargs)
            return instance

        monkeypatch.setattr("ocabox_tcs.services.tic_bridge_svc.client_pool.ClientAPI", factory)
        return calls

    @pytest.mark.asyncio
    async def test_initialize_requires_host_port(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host=None, port=None)
        # Patch the loader method to simulate failure to obtain host/port.
        with patch.object(pool, "_load_tic_endpoint_from_nats", new=AsyncMock()):
            with pytest.raises(RuntimeError, match="TIC host/port unavailable"):
                await pool.initialize()

    @pytest.mark.asyncio
    async def test_initialize_creates_default_client(
        self, fake_client_cls, fake_clientapi_cls, logger
    ):
        pool = ClientAPIPool(
            logger=logger, host="localhost", port=5559, default_client_name="bridge"
        )
        await pool.initialize()
        assert pool.size == 1  # default entry
        assert fake_clientapi_cls[0]["user_name"] == "bridge"

    @pytest.mark.asyncio
    async def test_get_default_reuses(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1)
        await pool.initialize()
        a = await pool.get(None)
        b = await pool.get(None)
        c = await pool.get("")  # empty string = default
        assert a is b is c
        assert pool.size == 1

    @pytest.mark.asyncio
    async def test_get_creates_per_client_id(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1)
        await pool.initialize()
        alice = await pool.get("alice")
        bob = await pool.get("bob")
        alice2 = await pool.get("alice")
        assert alice is alice2
        assert alice is not bob
        assert pool.size == 3  # default + alice + bob

    @pytest.mark.asyncio
    async def test_client_id_passed_as_user_name(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1)
        await pool.initialize()
        await pool.get("operator_panel")
        # Last-created entry used operator_panel as user_name
        assert fake_clientapi_cls[-1]["user_name"] == "operator_panel"

    @pytest.mark.asyncio
    async def test_lru_eviction_preserves_default(
        self, fake_client_cls, fake_clientapi_cls, logger
    ):
        pool = ClientAPIPool(logger=logger, host="h", port=1, max_clients=3, client_ttl=0)
        await pool.initialize()  # default
        await pool.get("c1")
        await pool.get("c2")
        # At capacity: adding c3 evicts the LRU non-default (c1)
        await pool.get("c1")  # bump c1 to be MRU
        await pool.get("c3")  # should evict c2 (now LRU)
        assert pool.size == 3
        keys = set(pool._pool.keys())
        assert ClientAPIPool.DEFAULT_KEY in keys
        assert "c1" in keys
        assert "c3" in keys
        assert "c2" not in keys

    @pytest.mark.asyncio
    async def test_ttl_eviction(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1, client_ttl=0.01)
        await pool.initialize()
        await pool.get("short-lived")
        assert pool.size == 2
        await asyncio.sleep(0.02)
        await pool.get("another")  # triggers TTL sweep
        keys = set(pool._pool.keys())
        assert "short-lived" not in keys
        assert "another" in keys

    @pytest.mark.asyncio
    async def test_close_clears_pool(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1)
        await pool.initialize()
        await pool.get("x")
        await pool.close()
        assert pool.size == 0

    @pytest.mark.asyncio
    async def test_get_before_initialize_raises(self, fake_client_cls, fake_clientapi_cls, logger):
        pool = ClientAPIPool(logger=logger, host="h", port=1)
        with pytest.raises(RuntimeError):
            await pool.get("x")


# ------------------------------------------------------------ command handler


class TestBridgeHandlerCommand:
    @pytest.fixture
    def pool_mock(self):
        p = MagicMock()
        p.get = AsyncMock()
        return p

    @pytest.fixture
    def handler(self, pool_mock, logger):
        return BridgeHandler(
            pool=pool_mock,
            command_prefix="tic.command",
            rpc_prefix="tic.rpc",
            default_request_timeout=5.0,
            sender_id="tic_bridge_svc.tic_bridge.main",
            logger=logger,
        )

    @pytest.mark.asyncio
    async def test_happy_path(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(return_value=_response(status=True, value_v=None))
        pool_mock.get.return_value = api

        cont = await handler.handle_command(
            data={"parameters": {"foo": "bar"}, "client_id": "op1"},
            meta={"nats": {"subject": "tic.command.T1.mount.slewtoaltaz"}},
        )
        assert cont is True
        pool_mock.get.assert_awaited_once_with("op1")
        api.put_async.assert_awaited_once()
        call = api.put_async.await_args
        assert call.args[0] == "T1.mount.slewtoaltaz"
        assert call.kwargs["parameters_dict"] == {"foo": "bar"}
        assert call.kwargs["no_wait"] is False
        assert handler.recent_error_count == 0

    @pytest.mark.asyncio
    async def test_missing_subject(self, handler, pool_mock):
        cont = await handler.handle_command(data={}, meta={})
        assert cont is True
        pool_mock.get.assert_not_called()
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_malformed_subject(self, handler, pool_mock):
        cont = await handler.handle_command(data={}, meta={"nats": {"subject": "other.prefix.x"}})
        assert cont is True
        pool_mock.get.assert_not_called()
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_timeout_recorded(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(side_effect=CommunicationTimeoutError("boom"))
        pool_mock.get.return_value = api
        await handler.handle_command(
            data={},
            meta={"nats": {"subject": "tic.command.T1.x"}},
        )
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_comm_error_recorded(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(side_effect=CommunicationRuntimeError("bad"))
        pool_mock.get.return_value = api
        await handler.handle_command(data={}, meta={"nats": {"subject": "tic.command.T1.x"}})
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_tic_rejection_recorded(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(
            return_value=_response(status=False, err_code=1004, err_msg="denied")
        )
        pool_mock.get.return_value = api
        await handler.handle_command(data={}, meta={"nats": {"subject": "tic.command.T1.mount.x"}})
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_pool_failure_recorded(self, handler, pool_mock):
        pool_mock.get.side_effect = RuntimeError("exhausted")
        await handler.handle_command(
            data={"client_id": "z"}, meta={"nats": {"subject": "tic.command.T1.x"}}
        )
        assert handler.recent_error_count == 1

    @pytest.mark.asyncio
    async def test_request_timeout_absolute(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(return_value=_response())
        pool_mock.get.return_value = api
        await handler.handle_command(
            data={"request_timeout": 2.5},
            meta={"nats": {"subject": "tic.command.T1.x"}},
        )
        # request_timeout arg is absolute epoch; should be roughly "now + 2.5"
        import time as _time

        absolute = api.put_async.await_args.kwargs["request_timeout"]
        assert abs(absolute - (_time.time() + 2.5)) < 0.5


# ---------------------------------------------------------------- rpc handler


def _make_rpc(subject: str, data: dict | None = None):
    nats_msg = SimpleNamespace(subject=subject, reply="inbox.x")
    rpc = SimpleNamespace(
        nats_msg=nats_msg,
        data=data,
        meta={},
        answered=False,
        resp_data=None,
        resp_meta=None,
    )

    def set_response(data=None, meta=None):
        rpc.resp_data = data
        rpc.resp_meta = meta

    rpc.set_response = set_response
    return rpc


class TestBridgeHandlerRpc:
    @pytest.fixture
    def pool_mock(self):
        p = MagicMock()
        p.get = AsyncMock()
        return p

    @pytest.fixture
    def handler(self, pool_mock, logger):
        return BridgeHandler(
            pool=pool_mock,
            command_prefix="tic.command",
            rpc_prefix="tic.rpc",
            default_request_timeout=5.0,
            sender_id="tic_bridge.main",
            logger=logger,
        )

    @pytest.mark.asyncio
    async def test_get_default(self, handler, pool_mock):
        api = MagicMock()
        api.get_async = AsyncMock(return_value=_response(status=True, value_v=42.0))
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.mount.rightascension", data={})
        await handler.handle_rpc(rpc)

        api.get_async.assert_awaited_once()
        assert rpc.resp_data["status"] == "ok"
        assert rpc.resp_data["result"] == 42.0
        assert rpc.resp_meta["message_type"] == "rpc"
        assert rpc.resp_meta["sender"] == "tic_bridge.main"

    @pytest.mark.asyncio
    async def test_put_explicit(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(return_value=_response(status=True, value_v=True))
        pool_mock.get.return_value = api

        rpc = _make_rpc(
            "tic.rpc.T1.mount.tracking", data={"method": "PUT", "parameters": {"Tracking": True}}
        )
        await handler.handle_rpc(rpc)

        api.put_async.assert_awaited_once()
        assert rpc.resp_data["status"] == "ok"
        assert rpc.resp_data["result"] is True

    @pytest.mark.asyncio
    async def test_access_denied_maps_to_access_denied(self, handler, pool_mock):
        api = MagicMock()
        api.get_async = AsyncMock(
            return_value=_response(status=False, err_code=1004, err_msg="no access")
        )
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.mount.ra")
        await handler.handle_rpc(rpc)

        assert rpc.resp_data["status"] == "error"
        assert rpc.resp_data["error"] == "access_denied"
        assert rpc.resp_data["code"] == 1004

    @pytest.mark.asyncio
    async def test_safety_cutoff_maps_to_safety_cutoff(self, handler, pool_mock):
        api = MagicMock()
        api.put_async = AsyncMock(
            return_value=_response(status=False, err_code=1005, err_msg="cutoff engaged")
        )
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.mount.slewtoaltaz", data={"method": "PUT"})
        await handler.handle_rpc(rpc)

        assert rpc.resp_data["error"] == "safety_cutoff"
        assert rpc.resp_data["code"] == 1005

    @pytest.mark.asyncio
    async def test_timeout(self, handler, pool_mock):
        api = MagicMock()
        api.get_async = AsyncMock(side_effect=CommunicationTimeoutError("t/o"))
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.x")
        await handler.handle_rpc(rpc)

        assert rpc.resp_data["status"] == "error"
        assert rpc.resp_data["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_comm_error(self, handler, pool_mock):
        api = MagicMock()
        api.get_async = AsyncMock(side_effect=CommunicationRuntimeError("bad"))
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.x")
        await handler.handle_rpc(rpc)

        assert rpc.resp_data["error"] == "comm_error"

    @pytest.mark.asyncio
    async def test_invalid_subject(self, handler, pool_mock):
        rpc = _make_rpc("other.x")
        await handler.handle_rpc(rpc)
        assert rpc.resp_data["error"] == "invalid_request"
        pool_mock.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_method(self, handler, pool_mock):
        rpc = _make_rpc("tic.rpc.T1.x", data={"method": "DELETE"})
        await handler.handle_rpc(rpc)
        assert rpc.resp_data["error"] == "invalid_request"
        pool_mock.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_exhausted(self, handler, pool_mock):
        pool_mock.get.side_effect = RuntimeError("no room")
        rpc = _make_rpc("tic.rpc.T1.x")
        await handler.handle_rpc(rpc)
        assert rpc.resp_data["error"] == "pool_exhausted"

    @pytest.mark.asyncio
    async def test_sender_passes_client_id(self, handler, pool_mock):
        api = MagicMock()
        api.get_async = AsyncMock(return_value=_response(status=True, value_v=1))
        pool_mock.get.return_value = api

        rpc = _make_rpc("tic.rpc.T1.x", data={"client_id": "alice"})
        await handler.handle_rpc(rpc)
        pool_mock.get.assert_awaited_once_with("alice")


# -------------------------------------------------------------- service tests


class TestTicBridgeService:
    @pytest.fixture
    def svc(self, logger):
        s = TicBridgeService()
        s.svc_logger = logger
        s.svc_config = TicBridgeConfig(
            type="tic_bridge_svc.tic_bridge",
            variant="main",
            tic_host="localhost",
            tic_port=5559,
        )
        # Monitor stub — healthcheck registration & manual status are noop here.
        s.controller = MagicMock()
        s.controller.monitor = MagicMock()
        s.controller.monitor.add_healthcheck_cb = MagicMock()
        return s

    @pytest.mark.asyncio
    async def test_start_stop_wires_and_unwires(self, svc):
        # Patch pool + serverish factories so no real sockets / NATS I/O happen.
        pool_instance = MagicMock()
        pool_instance.initialize = AsyncMock()
        pool_instance.close = AsyncMock()
        pool_ctor = MagicMock(return_value=pool_instance)

        sub_instance = MagicMock()
        sub_instance.open = AsyncMock()
        sub_instance.subscribe = AsyncMock()
        sub_instance.close = AsyncMock()
        sub_factory = MagicMock(return_value=sub_instance)

        rpc_instance = MagicMock()
        rpc_instance.open = AsyncMock()
        rpc_instance.register_function = AsyncMock()
        rpc_instance.close = AsyncMock()
        rpc_factory = MagicMock(return_value=rpc_instance)

        with patch("ocabox_tcs.services.tic_bridge_svc.tic_bridge.ClientAPIPool", pool_ctor), patch(
            "ocabox_tcs.services.tic_bridge_svc.tic_bridge.get_callbacksubscriber", sub_factory
        ), patch("ocabox_tcs.services.tic_bridge_svc.tic_bridge.get_rpcresponder", rpc_factory):
            await svc.start_service()

            pool_ctor.assert_called_once()
            pool_instance.initialize.assert_awaited_once()
            sub_factory.assert_called_once_with("tic.command.>", deliver_policy="new")
            sub_instance.open.assert_awaited_once()
            sub_instance.subscribe.assert_awaited_once()
            rpc_factory.assert_called_once_with(subject="tic.rpc.>")
            rpc_instance.open.assert_awaited_once()
            rpc_instance.register_function.assert_awaited_once()
            svc.controller.monitor.add_healthcheck_cb.assert_called_once()

            await svc.stop_service()

            rpc_instance.close.assert_awaited_once()
            sub_instance.close.assert_awaited_once()
            pool_instance.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disable_rpc(self, svc):
        svc.svc_config = TicBridgeConfig(
            type="tic_bridge_svc.tic_bridge",
            variant="main",
            tic_host="h",
            tic_port=1,
            enable_rpc=False,
        )

        pool_instance = MagicMock()
        pool_instance.initialize = AsyncMock()
        pool_instance.close = AsyncMock()
        sub_instance = MagicMock()
        sub_instance.open = AsyncMock()
        sub_instance.subscribe = AsyncMock()
        sub_instance.close = AsyncMock()

        with patch(
            "ocabox_tcs.services.tic_bridge_svc.tic_bridge.ClientAPIPool",
            MagicMock(return_value=pool_instance),
        ), patch(
            "ocabox_tcs.services.tic_bridge_svc.tic_bridge.get_callbacksubscriber",
            MagicMock(return_value=sub_instance),
        ), patch(
            "ocabox_tcs.services.tic_bridge_svc.tic_bridge.get_rpcresponder"
        ) as rpc_factory:
            await svc.start_service()
            rpc_factory.assert_not_called()
            await svc.stop_service()

    @pytest.mark.asyncio
    async def test_healthcheck_threshold(self, svc):
        # Manually wire a handler with bumped errors and confirm healthcheck reports DEGRADED.
        from ocabox_tcs.monitoring import Status

        handler = BridgeHandler(
            pool=MagicMock(),
            command_prefix="tic.command",
            rpc_prefix="tic.rpc",
            default_request_timeout=5.0,
            sender_id="s",
            logger=logging.getLogger("t"),
        )
        svc._handler = handler
        assert svc._healthcheck() is None
        for _ in range(BridgeHandler.ERROR_DEGRADED_THRESHOLD):
            handler._record_error()
        assert svc._healthcheck() is Status.DEGRADED
