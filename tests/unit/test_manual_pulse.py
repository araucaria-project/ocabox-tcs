"""Unit tests for `Controller.manual_pulse` (G1) — operator/UI hand-pulses
that bypass the Solver/Enforcer chain.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ocabox_tcs.services.guiding_svc.controller import Controller
from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.stages.solver.methods.dummy import DummyMethod
from ocabox_tcs.services.guiding_svc.state import Mode, PipelineState


def _make_pipeline(*, mount=None) -> Pipeline:
    state = PipelineState(pipeline_id="mon", camera_id="cam", mode=Mode.OFF)
    collector = MagicMock()
    return Pipeline(
        initial_state=state,
        collector=collector,
        method=DummyMethod(),
        queue_depth=2,
        mount=mount,
    )


def _make_mount() -> MagicMock:
    mount = MagicMock()
    mount.aput_pulseguide = AsyncMock()
    return mount


# ---------------------------------------------------------------------------
# Direction codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction,expected", [(0, 0), (1, 1), (2, 2), (3, 3)])
@pytest.mark.asyncio
async def test_manual_pulse_accepts_int_directions(direction, expected):
    mount = _make_mount()
    ctrl = Controller(_make_pipeline(mount=mount))
    out = await ctrl.manual_pulse(direction=direction, duration_ms=200)
    assert out["status"] == "ok"
    assert out["direction"] == expected
    mount.aput_pulseguide.assert_awaited_once_with(direction=expected, duration=200.0)


@pytest.mark.parametrize(
    "letter,expected_code,expected_label",
    [("N", 0, "N"), ("s", 1, "S"), (" e ", 2, "E"), ("W", 3, "W")],
)
@pytest.mark.asyncio
async def test_manual_pulse_accepts_letter_directions(letter, expected_code, expected_label):
    mount = _make_mount()
    ctrl = Controller(_make_pipeline(mount=mount))
    out = await ctrl.manual_pulse(direction=letter, duration_ms=100)
    assert out["direction"] == expected_code
    assert out["direction_label"] == expected_label
    mount.aput_pulseguide.assert_awaited_once_with(
        direction=expected_code, duration=100.0
    )


@pytest.mark.parametrize("bad_dir", [-1, 4, 99, "X", "north", "", None])
@pytest.mark.asyncio
async def test_manual_pulse_rejects_invalid_direction(bad_dir):
    ctrl = Controller(_make_pipeline(mount=_make_mount()))
    with pytest.raises(ValueError, match="direction must be"):
        await ctrl.manual_pulse(direction=bad_dir, duration_ms=100)


# ---------------------------------------------------------------------------
# Duration validation + safety cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_pulse_rejects_negative_duration():
    ctrl = Controller(_make_pipeline(mount=_make_mount()))
    with pytest.raises(ValueError, match="duration_ms must be ≥ 0"):
        await ctrl.manual_pulse(direction=0, duration_ms=-100)


@pytest.mark.asyncio
async def test_manual_pulse_rejects_non_numeric_duration():
    ctrl = Controller(_make_pipeline(mount=_make_mount()))
    with pytest.raises(ValueError, match="duration_ms must be numeric"):
        await ctrl.manual_pulse(direction=0, duration_ms="huge")


@pytest.mark.asyncio
async def test_manual_pulse_rejects_above_safety_cap():
    """5000ms hard cap to protect against finger-fumbles."""
    ctrl = Controller(_make_pipeline(mount=_make_mount()))
    with pytest.raises(ValueError, match="exceeds manual-pulse cap"):
        await ctrl.manual_pulse(direction=0, duration_ms=10_000)


@pytest.mark.asyncio
async def test_manual_pulse_at_cap_is_accepted():
    mount = _make_mount()
    ctrl = Controller(_make_pipeline(mount=mount))
    out = await ctrl.manual_pulse(direction=0, duration_ms=5000)
    assert out["status"] == "ok"
    mount.aput_pulseguide.assert_awaited_once_with(direction=0, duration=5000.0)


# ---------------------------------------------------------------------------
# No-mount fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_pulse_no_mount_returns_status(caplog):
    ctrl = Controller(_make_pipeline(mount=None))
    with caplog.at_level(logging.INFO, logger="controller"):
        out = await ctrl.manual_pulse(direction=0, duration_ms=200)
    assert out["status"] == "no_mount"
    assert out["direction"] == 0
    assert out["duration_ms"] == 200.0


# ---------------------------------------------------------------------------
# Telemetry — journal + event publishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_pulse_publishes_journal_entry():
    mount = _make_mount()
    ctrl = Controller(_make_pipeline(mount=mount))
    # MsgJournalPublisher uses a logging-like interface (.info, .warning,
    # .error) rather than .publish — the controller calls
    # ``journal_publisher.info(message)``. Mock the same shape.
    journal = AsyncMock()
    ctrl.journal_publisher = journal
    await ctrl.manual_pulse(direction=2, duration_ms=300)
    journal.info.assert_awaited_once()
    msg = journal.info.await_args.args[0]
    assert "manual_pulse E" in msg
    assert "300" in msg


@pytest.mark.asyncio
async def test_manual_pulse_publishes_event():
    mount = _make_mount()
    ctrl = Controller(_make_pipeline(mount=mount))
    events = AsyncMock()
    ctrl.events_publisher = events
    await ctrl.manual_pulse(direction=3, duration_ms=150)
    events.publish.assert_awaited_once()
    payload = events.publish.await_args.kwargs["data"]
    assert payload["event"] == "manual_pulse"
    assert payload["payload"] == {
        "direction": 3,
        "direction_label": "W",
        "duration_ms": 150.0,
    }


# ---------------------------------------------------------------------------
# Bypass behaviour — does NOT touch state, Solver, or Enforcer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_pulse_does_not_mutate_pipeline_state():
    """The whole point: manual pulses go around the state-mutation path."""
    pipe = _make_pipeline(mount=_make_mount())
    ctrl = Controller(pipe)
    state_before = pipe.state.snapshot()
    await ctrl.manual_pulse(direction=0, duration_ms=200)
    state_after = pipe.state.snapshot()
    # version should not have advanced — manual_pulse doesn't go through
    # set_state / Controller mutators.
    assert state_after.version == state_before.version


# ---------------------------------------------------------------------------
# RPC vocabulary
# ---------------------------------------------------------------------------


def test_manual_pulse_is_in_rpc_commands():
    from ocabox_tcs.services.guiding_svc.nats_conn import RPC_COMMANDS
    assert "manual_pulse" in RPC_COMMANDS
