"""Pulse-guide model — guider-side assembly of ``auto_adjust`` adapters.

Provides factories that turn pulse-guide-specific config (kN/kE, etc.)
into ``AdaptiveTransform`` instances. Domain semantics (pixels →
milliseconds, sign conventions) live here, not in ``auto_adjust``.

FL1 ships the trivial fixed-Jacobian path; later phases add probe-and-fit
bootstrap (``ActiveCalAdapter``) and online refinement (``RLSAdapter``).
"""

from __future__ import annotations

from typing import Any

from auto_adjust import AdaptiveTransform
from auto_adjust.adapters import FixedLinearAdapter


def build_fixed_jacobian_pulse_guide(
    *,
    kN_px_per_ms: float,
    kE_px_per_ms: float,
    sigma: float = 0.0,
) -> AdaptiveTransform:
    """Build a fixed inverse-Jacobian pulse-guide model from hand-calibrated
    sensitivities (FL1 path).

    Forward (pulse → star motion in sensor frame, orthogonal axes):
        dx_px = kE_px_per_ms * t_E_ms
        dy_px = kN_px_per_ms * t_N_ms

    Predict direction (error → pulse to *cancel* the error):
        t_N_ms = -dy_err_px / kN_px_per_ms
        t_E_ms = -dx_err_px / kE_px_per_ms

    The 2×2 inverse-with-correction matrix this assembles:
        [t_N]   [   0           -1/kN ] [dx_err]
        [t_E] = [-1/kE            0   ] [dy_err]

    Sign of the output components selects N vs S / E vs W in the Enforcer.

    Args:
        kN_px_per_ms: Star motion in pixel-y per ms of N pulse (signed,
            from hand calibration on first night).
        kE_px_per_ms: Star motion in pixel-x per ms of E pulse (signed).
        sigma: Predictive uncertainty surfaced via ``Prediction.sigma``.
    """
    if kN_px_per_ms == 0 or kE_px_per_ms == 0:
        raise ValueError("kN_px_per_ms and kE_px_per_ms must be nonzero")
    return FixedLinearAdapter(
        m11=0.0,
        m12=-1.0 / kN_px_per_ms,
        m21=-1.0 / kE_px_per_ms,
        m22=0.0,
        sigma=sigma,
    )


def build_pulse_guide_model(config: dict[str, Any]) -> AdaptiveTransform:
    """Top-level factory — dispatches on config to FL1 / FL2 / FL3 paths.

    FL1: ``{"jacobian": {"kN_px_per_ms": ..., "kE_px_per_ms": ...}}``.
    FL2 (active calibration bootstrap) and FL3 (online RLS refinement)
    raise ``NotImplementedError`` until those adapters land.
    """
    jac = config.get("jacobian") or {}
    if "kN_px_per_ms" in jac and "kE_px_per_ms" in jac:
        return build_fixed_jacobian_pulse_guide(
            kN_px_per_ms=float(jac["kN_px_per_ms"]),
            kE_px_per_ms=float(jac["kE_px_per_ms"]),
            sigma=float(config.get("sigma", 0.0)),
        )
    raise NotImplementedError(
        "build_pulse_guide_model: only the fixed-jacobian FL1 path is "
        "implemented. Add 'jacobian.kN_px_per_ms' and 'kE_px_per_ms' to "
        "the per-pipeline config; later phases add ActiveCal + RLS."
    )
