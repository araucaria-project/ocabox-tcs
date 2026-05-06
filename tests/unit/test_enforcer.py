"""Unit tests for Enforcer (Track D — pulse-guide application)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from serverish.base import dt_utcnow_array

from auto_adjust.stability import DampingGuard, SaturationGuard
from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.pulse_guide import (
    build_fixed_jacobian_pulse_guide,
)
from ocabox_tcs.services.guiding_svc.stages.enforcer import (
    DIR_E,
    DIR_N,
    DIR_S,
    DIR_W,
    Enforcer,
)
from ocabox_tcs.services.guiding_svc.state import (
    Mode,
    PipelineState,
    PipelineStateHolder,
)


def _state_holder(mode: Mode = Mode.GUIDING) -> PipelineStateHolder:
    return PipelineStateHolder(
        PipelineState(pipeline_id="mon", camera_id="cam", mode=mode)
    )


def _correction(dx: float, dy: float) -> Correction:
    return Correction(
        dx_px=dx,
        dy_px=dy,
        method="single_star",
        confidence=1.0,
        timestamp=dt_utcnow_array(),
    )


def _make_mount() -> MagicMock:
    mount = MagicMock()
    mount.aput_pulseguide = AsyncMock()
    return mount


def _make_enforcer(
    *,
    mount=None,
    pulse_guide_model=None,
    damping=None,
    saturation_ms=None,
    min_pulse_ms: float = 20.0,
    mode: Mode = Mode.GUIDING,
):
    """Build an Enforcer with a one-shot queue prefilled with a Correction.

    The caller pushes the test correction onto in_queue and runs `_apply`
    directly (skipping the run-loop) for deterministic single-frame tests.
    """
    state = _state_holder(mode=mode)
    in_q: asyncio.Queue[Correction] = asyncio.Queue(maxsize=4)
    return Enforcer(
        in_queue=in_q,
        state=state,
        mount=mount,
        pulse_guide_model=pulse_guide_model,
        damping=damping,
        saturation_ms=saturation_ms,
        min_pulse_ms=min_pulse_ms,
    )


# ---------------------------------------------------------------------------
# log-only paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_logs_only_when_no_model(caplog):
    """No pulse_guide_model → log-only, mount untouched."""
    mount = _make_mount()
    enf = _make_enforcer(mount=mount, pulse_guide_model=None)
    with caplog.at_level(logging.INFO, logger="enforcer"):
        await enf._apply(_correction(3.0, 4.0))
    assert any("no model" in rec.message for rec in caplog.records)
    mount.aput_pulseguide.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_logs_only_when_no_mount(caplog):
    """Model present but mount=None → log-only."""
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(mount=None, pulse_guide_model=model)
    with caplog.at_level(logging.INFO, logger="enforcer"):
        await enf._apply(_correction(3.0, 4.0))
    assert any("log-only" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Direction selection (sign of t_N / t_E → N/S / E/W)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_y_error_pulses_south_when_t_n_negative():
    """dy=10 px, kN>0 → t_N=-500ms → pulse SOUTH for 500 ms."""
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
    )
    await enf._apply(_correction(0.0, 10.0))

    calls = mount.aput_pulseguide.await_args_list
    # Only one pulse (the E component is zero; below min_pulse_ms threshold).
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["direction"] == DIR_S
    assert kwargs["duration"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_pure_x_error_pulses_west_when_t_e_negative():
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
    )
    await enf._apply(_correction(5.0, 0.0))

    calls = mount.aput_pulseguide.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["direction"] == DIR_W
    assert calls[0].kwargs["duration"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_negative_y_error_pulses_north():
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
    )
    await enf._apply(_correction(0.0, -10.0))

    assert mount.aput_pulseguide.await_args_list[0].kwargs["direction"] == DIR_N


@pytest.mark.asyncio
async def test_negative_x_error_pulses_east():
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
    )
    await enf._apply(_correction(-5.0, 0.0))

    assert mount.aput_pulseguide.await_args_list[0].kwargs["direction"] == DIR_E


@pytest.mark.asyncio
async def test_diagonal_error_issues_both_axes():
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
    )
    # dx=5, dy=10 — both nonzero, both above min
    await enf._apply(_correction(5.0, 10.0))

    dirs = [c.kwargs["direction"] for c in mount.aput_pulseguide.await_args_list]
    assert DIR_S in dirs
    assert DIR_W in dirs
    assert len(dirs) == 2


# ---------------------------------------------------------------------------
# Stability guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_damping_halves_pulses_at_alpha_half():
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=0.5, alpha_max=0.5),
    )
    await enf._apply(_correction(0.0, 10.0))

    # Without damping → 500ms; with α=0.5 → 250ms.
    assert mount.aput_pulseguide.await_args_list[0].kwargs["duration"] == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_saturation_clips_huge_pulses(caplog):
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
        saturation_ms=SaturationGuard(lo=-1500.0, hi=1500.0),
    )
    # dy=100 px → -5000ms unclamped → clipped to -1500ms → S 1500ms
    with caplog.at_level(logging.WARNING, logger="enforcer"):
        await enf._apply(_correction(0.0, 100.0))
    assert mount.aput_pulseguide.await_args_list[0].kwargs["duration"] == pytest.approx(1500.0)
    assert any("saturation clipped" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_below_min_pulse_axis_is_skipped():
    """Sub-threshold pulses are suppressed (mount can't track micro-moves)."""
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount,
        pulse_guide_model=model,
        damping=DampingGuard(alpha_min=1.0, alpha_max=1.0),
        min_pulse_ms=20.0,
    )
    # dy=0.1 → -5ms → below 20ms threshold → skipped
    await enf._apply(_correction(0.0, 0.1))
    mount.aput_pulseguide.assert_not_awaited()


# ---------------------------------------------------------------------------
# Mode gating (only emit pulses when mode == GUIDING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_skips_when_mode_is_monitoring():
    """The dispatch in `_run` shouldn't call `_apply` outside guiding mode."""
    mount = _make_mount()
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    enf = _make_enforcer(
        mount=mount, pulse_guide_model=model, mode=Mode.MONITORING
    )
    await enf.in_queue.put(_correction(0.0, 10.0))

    await enf.start()
    # Give the run loop a moment to consume the queue.
    await asyncio.sleep(0.05)
    await enf.stop()

    mount.aput_pulseguide.assert_not_awaited()
