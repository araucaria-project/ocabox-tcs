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
    sensitivities (FL1 path — diagonal Jacobian, assumes N drives only Y
    and E drives only X).

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


def build_full_jacobian_pulse_guide(
    *,
    kE_x: float, kE_y: float,
    kN_x: float, kN_y: float,
    sigma: float = 0.0,
) -> AdaptiveTransform:
    """Build an inverse-Jacobian pulse-guide model from the full 2×2
    sensitivity matrix (FL2 path).

    Forward — pulse → star motion in sensor frame, allowing arbitrary
    orientation between mount axes and detector axes:

        [dx]   [kE_x  kN_x] [t_E]
        [dy] = [kE_y  kN_y] [t_N]

    To cancel an error vector ``(dx_err, dy_err)`` we want pulses such
    that the resulting motion is ``-error``. With ``J`` the forward
    matrix above and ``J⁻¹`` its inverse:

        [t_E]              [-dx_err]
        [t_N] = J⁻¹ @       [-dy_err]

    Working out the sign-corrected components and re-ordering to the
    adapter's ``[t_N, t_E] = M @ [dx_err, dy_err]`` convention gives:

        det = kE_x · kN_y - kN_x · kE_y
        m11 =  kE_y / det      (t_N coeff on dx_err)
        m12 = -kE_x / det
        m21 = -kN_y / det      (t_E coeff on dx_err)
        m22 =  kN_x / det

    The diagonal/orthogonal special case (``kE_y = kN_x = 0``)
    reproduces the FL1 model exactly — useful sanity check.

    Args:
        kE_x, kE_y: Star motion in (x, y) pixels per ms of E pulse.
        kN_x, kN_y: Star motion in (x, y) pixels per ms of N pulse.
        sigma: Predictive uncertainty surfaced via ``Prediction.sigma``.

    Raises:
        ValueError: when the calibrated mount axes are nearly parallel
            on the detector (``|sin(angle)| < 0.1``, i.e. < ~6° apart).
            Beyond a sharp ``det == 0`` test, near-parallel axes blow
            up the inverse — small dy errors translate to enormous
            pulse durations on whichever axis happens to dominate. A
            healthy guider needs the two mount axes to map to clearly
            distinguishable directions on the chip; if calibration
            returns near-parallel ks, the data is contaminated (e.g.
            backlash + short settle, or axis-parallel mount geometry)
            and we refuse rather than emit unstable corrections.
    """
    kE_x_f = float(kE_x); kE_y_f = float(kE_y)
    kN_x_f = float(kN_x); kN_y_f = float(kN_y)
    det = kE_x_f * kN_y_f - kN_x_f * kE_y_f
    n_mag = (kN_x_f * kN_x_f + kN_y_f * kN_y_f) ** 0.5
    e_mag = (kE_x_f * kE_x_f + kE_y_f * kE_y_f) ** 0.5
    if n_mag == 0.0 or e_mag == 0.0:
        raise ValueError(
            f"full-Jacobian has a zero-magnitude axis "
            f"(|N|={n_mag:.6f}, |E|={e_mag:.6f}) — calibration probe "
            f"recorded no motion in at least one direction. Re-run "
            f"calibration with longer pulses + longer settle."
        )
    sin_angle = abs(det) / (n_mag * e_mag)
    if sin_angle < 0.1:
        raise ValueError(
            f"full-Jacobian axes are nearly parallel "
            f"(sin(angle)={sin_angle:.3f}, ~{(sin_angle * 57.3):.1f}°) — "
            f"the inverse would explode small drift errors into huge "
            f"pulses. Check the calibration: at least one set of probes "
            f"is likely contaminated (mount backlash, short settle, "
            f"lock-hopping). kE=({kE_x_f}, {kE_y_f}) kN=({kN_x_f}, {kN_y_f})"
        )
    return FixedLinearAdapter(
        m11=float(kE_y) / det,
        m12=-float(kE_x) / det,
        m21=-float(kN_y) / det,
        m22=float(kN_x) / det,
        sigma=sigma,
    )


def build_pulse_guide_model(config: dict[str, Any]) -> AdaptiveTransform:
    """Top-level factory — dispatches on config to FL1 / FL2 / FL3 paths.

    FL2 (full 2×2): all four ``kE_x kE_y kN_x kN_y`` keys present —
        wins over FL1. Result of an actual calibration probe sequence.
    FL1 (diagonal fallback): just ``kN_px_per_ms`` + ``kE_px_per_ms`` —
        assumes orthogonal axes; correct only if N pulses move star
        purely along pixel-Y and E pulses purely along pixel-X (rare
        in practice, depends on derotator + mount geometry + transpose).
    FL3 (online RLS refinement) raises ``NotImplementedError`` until
        that adapter lands.
    """
    jac = config.get("jacobian") or {}
    full_keys = ("kE_x_px_per_ms", "kE_y_px_per_ms", "kN_x_px_per_ms", "kN_y_px_per_ms")
    if all(k in jac for k in full_keys):
        return build_full_jacobian_pulse_guide(
            kE_x=float(jac["kE_x_px_per_ms"]),
            kE_y=float(jac["kE_y_px_per_ms"]),
            kN_x=float(jac["kN_x_px_per_ms"]),
            kN_y=float(jac["kN_y_px_per_ms"]),
            sigma=float(config.get("sigma", 0.0)),
        )
    if "kN_px_per_ms" in jac and "kE_px_per_ms" in jac:
        return build_fixed_jacobian_pulse_guide(
            kN_px_per_ms=float(jac["kN_px_per_ms"]),
            kE_px_per_ms=float(jac["kE_px_per_ms"]),
            sigma=float(config.get("sigma", 0.0)),
        )
    raise NotImplementedError(
        "build_pulse_guide_model: provide either full 2×2 Jacobian "
        "(kE_x_px_per_ms, kE_y_px_per_ms, kN_x_px_per_ms, kN_y_px_per_ms) "
        "or the FL1 diagonal pair (kN_px_per_ms, kE_px_per_ms). "
        "FL3 online refinement (RLS) lands later."
    )
