"""Unit tests for FixedLinearAdapter (auto_adjust) + the pulse-guide
domain helper that assembles it (``pulse_guide.build_fixed_jacobian_pulse_guide``).

The split mirrors the architectural boundary: ``auto_adjust`` is
field-agnostic; pixels-and-milliseconds semantics live on the guider side.
"""

from __future__ import annotations

import json
import time

import pytest

from auto_adjust.adapters import FixedLinearAdapter
from auto_adjust.base import Observation
from ocabox_tcs.services.guiding_svc.pulse_guide import (
    build_fixed_jacobian_pulse_guide,
    build_pulse_guide_model,
)


# ---------------------------------------------------------------------------
# FixedLinearAdapter — generic 2x2 linear map
# ---------------------------------------------------------------------------


def test_predict_applies_matrix():
    """y = M @ x with the 2x2 entries supplied at construction."""
    a = FixedLinearAdapter(m11=1.0, m12=2.0, m21=3.0, m22=4.0)
    p = a.predict((10.0, 1.0))
    assert p.y == pytest.approx((1 * 10 + 2 * 1, 3 * 10 + 4 * 1))


def test_predict_metadata_carries_matrix_entries():
    a = FixedLinearAdapter(m11=0.0, m12=-50.0, m21=-100.0, m22=0.0)
    p = a.predict((1.0, 2.0))
    assert p.is_calibrated is True
    assert p.metadata["m11"] == 0.0
    assert p.metadata["m12"] == -50.0


def test_is_calibrated_always_true():
    a = FixedLinearAdapter(m11=1.0, m12=0.0, m21=0.0, m22=1.0)
    assert a.is_calibrated() is True


def test_record_is_diagnostic_only():
    """The fixed adapter doesn't learn — record bumps a counter only."""
    a = FixedLinearAdapter(m11=1.0, m12=0.0, m21=0.0, m22=1.0)
    before = a.predict((1.0, 1.0)).y
    a.record(Observation(x=(1.0, 1.0), y_actual=(0.0, 0.0), timestamp=time.time()))
    a.record(Observation(x=(2.0, 3.0), y_actual=(0.0, 0.0), timestamp=time.time()))
    assert a.predict((1.0, 1.0)).y == before
    assert a.health_metrics()["n_samples"] == 2


def test_reset_clears_counters():
    a = FixedLinearAdapter(m11=1.0, m12=0.0, m21=0.0, m22=1.0)
    a.record(Observation(x=(1.0, 1.0), y_actual=(0.0, 0.0), timestamp=1.0))
    a.reset()
    assert a.health_metrics()["n_samples"] == 0


def test_serialise_round_trips():
    a = FixedLinearAdapter(m11=0.1, m12=0.2, m21=0.3, m22=0.4, sigma=0.5)
    payload = json.loads(a.serialise())
    assert payload["m11"] == 0.1
    assert payload["m22"] == 0.4
    assert payload["sigma"] == 0.5


def test_notify_event_is_noop():
    a = FixedLinearAdapter(m11=1.0, m12=0.0, m21=0.0, m22=1.0)
    a.notify_event("focus_shift", focuser_pos=12345)
    a.notify_event("meridian_flip")
    assert a.is_calibrated() is True


def test_health_metrics_shape():
    a = FixedLinearAdapter(m11=0.1, m12=0.0, m21=0.0, m22=0.2)
    h = a.health_metrics()
    assert h["model"] == "fixed_linear"
    assert h["calibrated"] is True
    assert h["m22"] == 0.2


# ---------------------------------------------------------------------------
# Pulse-guide domain assembly
# ---------------------------------------------------------------------------


def test_build_fixed_jacobian_pulse_guide_pure_y_error():
    """dy_err > 0 → t_N negative (Enforcer pulses S to bring star back)."""
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    t_N, t_E = model.predict((0.0, 10.0)).y
    assert t_N == pytest.approx(-500.0)  # -10 / 0.02
    assert t_E == pytest.approx(0.0)


def test_build_fixed_jacobian_pulse_guide_pure_x_error():
    model = build_fixed_jacobian_pulse_guide(
        kN_px_per_ms=0.02, kE_px_per_ms=0.01
    )
    t_N, t_E = model.predict((5.0, 0.0)).y
    assert t_E == pytest.approx(-500.0)  # -5 / 0.01
    assert t_N == pytest.approx(0.0)


def test_build_fixed_jacobian_pulse_guide_signs_invert_with_jacobian():
    """Negate hand-cal sensitivities (sign error) → resulting pulses invert."""
    a = build_fixed_jacobian_pulse_guide(kN_px_per_ms=0.02, kE_px_per_ms=0.01)
    b = build_fixed_jacobian_pulse_guide(kN_px_per_ms=-0.02, kE_px_per_ms=-0.01)
    pa = a.predict((3.0, 4.0)).y
    pb = b.predict((3.0, 4.0)).y
    assert pa[0] == pytest.approx(-pb[0])
    assert pa[1] == pytest.approx(-pb[1])


def test_build_fixed_jacobian_pulse_guide_zero_jacobian_rejected():
    with pytest.raises(ValueError, match="must be nonzero"):
        build_fixed_jacobian_pulse_guide(kN_px_per_ms=0.0, kE_px_per_ms=1.0)


def test_build_pulse_guide_model_dispatches_to_fixed():
    cfg = {"jacobian": {"kN_px_per_ms": 0.012, "kE_px_per_ms": 0.011}}
    model = build_pulse_guide_model(cfg)
    # FL1 path uses FixedLinearAdapter under the hood.
    assert isinstance(model, FixedLinearAdapter)


def test_build_pulse_guide_model_rejects_unconfigured():
    """No jacobian, no learning adapter requested — surfaces NotImplementedError."""
    with pytest.raises(NotImplementedError, match="provide either full 2×2"):
        build_pulse_guide_model({})
