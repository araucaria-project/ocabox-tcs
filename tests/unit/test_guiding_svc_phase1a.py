"""Unit tests for guiding_svc Phase 1A — RPC handler wiring + state mutation.

Covers:
- Mode enum (off/monitoring/guiding/live).
- Controller's RPC surface (set_state / set_mode / acquire / snapshot /
  dark_rebuild / bias_rebuild).
- nats_conn._wrap_handler envelope shape (status=ok / status=error).
- Publisher factory functions return None when Messenger isn't open.

No NATS required; integration tests with a real broker land in 1D.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ocabox_tcs.services.guiding_svc.controller import Controller
from ocabox_tcs.services.guiding_svc.nats_conn import (
    RPC_COMMANDS,
    NatsConn,
    _serialise,
    _wrap_handler,
)
from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.stages.solver.methods.dummy import DummyMethod
from ocabox_tcs.services.guiding_svc.state import Mode, PipelineState


@pytest.fixture
def make_pipeline():
    """Build a minimal Pipeline without a CameraArrayCollector subscription."""

    def _make(mode: Mode = Mode.OFF) -> Pipeline:
        state = PipelineState(pipeline_id="mon", camera_id="cam-A", mode=mode)
        # Pipeline.start() honours OFF mode and skips collector subscription;
        # we never call start() in these tests, so collector is unused.
        collector = MagicMock()
        return Pipeline(
            initial_state=state,
            collector=collector,
            method=DummyMethod(),
            queue_depth=2,
            mount=None,
        )

    return _make


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------


def test_mode_enum_includes_live():
    assert {m.value for m in Mode} == {"off", "monitoring", "guiding", "live"}


# ---------------------------------------------------------------------------
# Controller surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controller_set_state_returns_snapshot(make_pipeline):
    ctrl = Controller(make_pipeline())
    snap = await ctrl.set_state({"exp_time": 0.5, "binning": 2})
    assert snap["exp_time"] == 0.5
    assert snap["binning"] == 2
    assert snap["version"] == 1


@pytest.mark.asyncio
async def test_controller_set_mode_coerces_strings(make_pipeline):
    ctrl = Controller(make_pipeline())
    snap = await ctrl.set_mode("guiding")
    assert snap["mode"] == "guiding"
    snap2 = await ctrl.set_mode("live")
    assert snap2["mode"] == "live"


@pytest.mark.asyncio
async def test_controller_acquire_resets_lock(make_pipeline):
    pipe = make_pipeline()
    await pipe.state.update(acquired=True, acquired_pos=(100.0, 200.0))
    ctrl = Controller(pipe)
    snap = await ctrl.acquire()
    assert snap["acquired"] is False
    assert snap["acquired_pos"] is None


@pytest.mark.asyncio
async def test_controller_snapshot_is_readonly(make_pipeline):
    ctrl = Controller(make_pipeline(mode=Mode.MONITORING))
    snap = ctrl.snapshot()
    assert snap["mode"] == "monitoring"
    # Mutating the returned dict must not affect controller state
    snap["mode"] = "guiding"
    assert ctrl.snapshot()["mode"] == "monitoring"


@pytest.mark.asyncio
async def test_controller_dark_rebuild_is_phase4_stub(make_pipeline):
    ctrl = Controller(make_pipeline())
    result = await ctrl.dark_rebuild(n_frames=5)
    assert result == {
        "status": "queued",
        "implemented": False,
        "phase": "Phase 4",
        "params": {"n_frames": 5},
    }


@pytest.mark.asyncio
async def test_controller_bias_rebuild_is_phase4_stub(make_pipeline):
    ctrl = Controller(make_pipeline())
    result = await ctrl.bias_rebuild()
    assert result["status"] == "queued"
    assert result["phase"] == "Phase 4"


@pytest.mark.asyncio
async def test_controller_publishes_state_on_mutation(make_pipeline):
    ctrl = Controller(make_pipeline())
    pub = AsyncMock()
    ctrl.state_publisher = pub
    await ctrl.set_state({"exp_time": 0.25})
    pub.publish.assert_awaited_once()
    kwargs = pub.publish.await_args.kwargs
    assert kwargs["data"]["exp_time"] == 0.25
    # Controller currently uses serverish's generic ``"default"`` message_type
    # for state publishes. Distinct types per topic (e.g. ``guider.state``)
    # are a follow-up — at the wire level the channel + schema validation
    # already disambiguate. Test pins the actual value rather than the
    # aspirational one so it tracks production behaviour.
    assert kwargs["meta"]["message_type"] == "default"
    assert kwargs["meta"]["sender"]  # non-empty


@pytest.mark.asyncio
async def test_controller_emits_mode_changed_event(make_pipeline):
    ctrl = Controller(make_pipeline(mode=Mode.OFF))
    events = AsyncMock()
    ctrl.events_publisher = events
    await ctrl.set_mode("monitoring")
    # set_state publishes state once; mode change additionally publishes one event
    events.publish.assert_awaited_once()
    payload = events.publish.await_args.kwargs["data"]
    assert payload["event"] == "mode_changed"
    assert payload["payload"] == {"from": "off", "to": "monitoring"}


@pytest.mark.asyncio
async def test_controller_no_event_when_mode_unchanged(make_pipeline):
    ctrl = Controller(make_pipeline(mode=Mode.MONITORING))
    events = AsyncMock()
    ctrl.events_publisher = events
    await ctrl.set_mode("monitoring")
    events.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_journal_falls_back_to_logger_when_no_publisher(
    make_pipeline, caplog
):
    ctrl = Controller(make_pipeline())
    with caplog.at_level(logging.INFO, logger="controller"):
        await ctrl.dark_rebuild()
    assert any("dark_rebuild requested" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# exp_time / current_exp_time semantics — regression for the silent-shadow bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_state_exp_time_clears_auto_override(make_pipeline):
    """Operator changing the baseline must drop any active auto-exposure
    override so the operator value reaches the camera on the next frame."""
    pipe = make_pipeline()
    ctrl = Controller(pipe)
    # Simulate auto-exposure having taken control.
    await pipe.state.update(current_exp_time=2.5)
    assert pipe.state.snapshot().current_exp_time == 2.5

    await ctrl.set_state({"exp_time": 0.7})
    snap = pipe.state.snapshot()
    assert snap.exp_time == 0.7
    assert snap.current_exp_time is None


@pytest.mark.asyncio
async def test_set_state_preserves_explicit_current_exp_time(make_pipeline):
    """Auto-exposure may set both fields atomically; that explicit
    ``current_exp_time`` must not be clobbered by the convenience reset."""
    pipe = make_pipeline()
    ctrl = Controller(pipe)
    await ctrl.set_state({"exp_time": 0.7, "current_exp_time": 0.3})
    snap = pipe.state.snapshot()
    assert snap.exp_time == 0.7
    assert snap.current_exp_time == 0.3


def test_build_exposure_job_uses_baseline_when_no_override(make_pipeline):
    pipe = make_pipeline()
    asyncio.run(pipe.state.update(exp_time=0.4, current_exp_time=None))
    job = pipe._build_exposure_job()
    assert job.exp_time == 0.4


def test_build_exposure_job_uses_override_when_set(make_pipeline):
    pipe = make_pipeline()
    asyncio.run(pipe.state.update(exp_time=0.4, current_exp_time=1.7))
    job = pipe._build_exposure_job()
    assert job.exp_time == 1.7


# ---------------------------------------------------------------------------
# nats_conn handler shaping
# ---------------------------------------------------------------------------


def _make_rpc(data: dict[str, Any] | None = None) -> MagicMock:
    rpc = MagicMock()
    rpc.data = data or {}
    rpc.response_now = AsyncMock()
    return rpc


@pytest.mark.asyncio
async def test_wrap_handler_ok_envelope():
    async def inner_async(_data):
        return {"hello": "world"}

    handler = _wrap_handler("cam-A.mon", inner_async)
    rpc = _make_rpc({})
    await handler(rpc)
    rpc.response_now.assert_awaited_once()
    sent = rpc.response_now.await_args.kwargs["data"]
    assert sent["status"] == "ok"
    assert sent["result"] == {"hello": "world"}
    assert "ts" in sent
    meta = rpc.response_now.await_args.kwargs["meta"]
    assert meta == {"message_type": "rpc", "sender": "cam-A.mon"}


@pytest.mark.asyncio
async def test_wrap_handler_sync_inner_returns_value():
    def inner_sync(_data):
        return 42

    handler = _wrap_handler("cam-A.mon", inner_sync)
    rpc = _make_rpc()
    await handler(rpc)
    assert rpc.response_now.await_args.kwargs["data"]["result"] == 42


@pytest.mark.asyncio
async def test_wrap_handler_not_implemented_envelope():
    def inner(_data):
        raise NotImplementedError("come back in Phase 4")

    handler = _wrap_handler("cam-A.mon", inner)
    rpc = _make_rpc()
    await handler(rpc)
    sent = rpc.response_now.await_args.kwargs["data"]
    assert sent["status"] == "error"
    assert sent["error"] == "not_implemented"
    assert "Phase 4" in sent["detail"]


@pytest.mark.asyncio
async def test_wrap_handler_generic_error_envelope():
    def inner(_data):
        raise ValueError("bad input")

    handler = _wrap_handler("cam-A.mon", inner)
    rpc = _make_rpc()
    await handler(rpc)
    sent = rpc.response_now.await_args.kwargs["data"]
    assert sent["status"] == "error"
    assert sent["error"] == "ValueError"
    assert sent["detail"] == "bad input"


# ---------------------------------------------------------------------------
# nats_conn integration with Controller (no Messenger open)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_pipeline_rpcs_noop_without_messenger(make_pipeline):
    """When Messenger isn't open, register_pipeline_rpcs returns cleanly."""
    manager = MagicMock()
    manager.svc_logger = logging.getLogger("test")
    conn = NatsConn(manager=manager, instance="jk15.cam_test")
    assert conn.is_available is False
    ctrl = Controller(make_pipeline())
    # Must not raise — service starts fine without NATS.
    await conn.register_pipeline_rpcs("cam_test", "mon", ctrl)
    assert conn._responders == []


@pytest.mark.asyncio
async def test_publishers_return_none_without_messenger():
    manager = MagicMock()
    manager.svc_logger = logging.getLogger("test")
    conn = NatsConn(manager=manager, instance="jk15.cam_test")
    assert conn.state_publisher("cam_test", "mon") is None
    assert conn.events_publisher("cam_test", "mon") is None
    assert conn.journal_publisher("cam_test", "mon") is None
    assert conn.correction_publisher("cam_test", "mon") is None
    assert conn.camera_active_correction_publisher("cam_test") is None
    assert conn.thumbnail_notification_publisher("cam_test") is None


def test_subject_builders_follow_naming_convention():
    """Subjects compose as <prefix>.<kind>.<service>.<instance>...."""
    manager = MagicMock()
    manager.svc_logger = logging.getLogger("test")
    conn = NatsConn(
        manager=manager,
        subject_prefix="svc",
        service="guider",
        instance="jk15.guider_beso",
    )
    assert conn.rpc_subject("guiding", "set_state") == (
        "svc.rpc.guider.jk15.guider_beso.pipeline.guiding.v1.set_state"
    )
    assert conn.publish_subject("guiding", "state") == (
        "svc.publish.guider.jk15.guider_beso.pipeline.guiding.state"
    )
    assert conn.telemetry_subject("guiding", "correction") == (
        "svc.telemetry.guider.jk15.guider_beso.pipeline.guiding.correction"
    )


def test_rpc_commands_set_complete():
    """Commit the wire-level command vocabulary. The tuple is the
    public API contract — adding a command here is intentional, and
    bumps a major-version conversation with downstream UIs."""
    assert RPC_COMMANDS == (
        "set_state",
        "set_mode",
        "acquire",
        "acquire_at",
        "lock_at",
        "drop_to_reticle",
        "snapshot",
        "dark_rebuild",
        "bias_rebuild",
        "manual_pulse",
        "pulse_pixels",
        "calibrate_probe",
    )


# ---------------------------------------------------------------------------
# Serialiser
# ---------------------------------------------------------------------------


def test_serialise_handles_mode_enum():
    assert _serialise(Mode.LIVE) == "live"


def test_serialise_handles_to_dict():
    class Foo:
        def to_dict(self):
            return {"k": Mode.GUIDING}

    assert _serialise(Foo()) == {"k": "guiding"}


def test_serialise_passes_primitives():
    assert _serialise(None) is None
    assert _serialise(3.14) == 3.14
    assert _serialise("x") == "x"
    assert _serialise([1, Mode.OFF]) == [1, "off"]
