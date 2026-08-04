"""FiberPhotocentroidMethod — flux-weighted photocentroid around the
fiber entrance with piecewise-linear hole-bias compensation.

Spectrograph fiber feed scenario: the target sits on (or near) the fiber
hole. When centred, most of the light enters the fiber and only a small
halo of un-coupled flux reaches the detector. As the star drifts off
the hole, more flux escapes onto the wings; the *photocentroid* of that
escaping flux points away from the fiber, toward where the star
geometrically is.

.. warning::

   **The premise of the compensation below is wrong.** The hole removes
   the light nearest its own centre, so the photocentroid of what escapes
   is pushed *outward*: it **over**-shoots the true star offset by a
   factor of roughly 1.3–2.5 near the hole. It does not under-shoot, as
   the original rationale (kept below, because the code still implements
   it) assumed. The ramp therefore compensates in the wrong direction,
   and its dead zone suppresses corrections for offsets that matter.

   Deliberately left in place: this is on-sky-validated behaviour, and
   replacing it changes guiding, which needs its own on-sky test. Note
   that a correct target position and a narrower dead zone have to land
   together — a wide dead zone around a correct target is still wide, and
   a narrow one around a stale target still chases the wrong place. See
   ``doc/guider/reticle-target-detection.md``.

Original rationale, superseded but describing the implemented code:
a pure 1:1 correction from that photocentroid systematically *under*-shoots
the real offset: when the star is half-in / half-out of the hole, the
apparent photocentroid sits between the hole rim and the star centre,
not at the star itself. So we apply a geometric piecewise-linear
compensation:

    d_apparent ≤ fiber_radius_px
        → dead zone: ``d_corrected = 0`` (don't fight noise inside the
          hole; if we move at all here we'd be reacting to PSF asymmetry
          and the halo geometry).
    d_apparent ≥ fiber_radius_px · hole_zone_factor
        → full regime: ``d_corrected = d_apparent`` (star is fully out
          of the hole, photocentroid ≈ true offset, 1:1 correction).
    in between
        → linear ramp from 0 to ``fiber_radius_px · hole_zone_factor``
          as d_apparent goes from ``fiber_radius_px`` to
          ``fiber_radius_px · hole_zone_factor``.

The tuning knob is ``hole_zone_factor`` (default 2.0). Lower = more
aggressive (correction kicks in sooner, more overshoot risk). Higher =
more conservative (more undershoot, slower convergence, fewer oscillations).

The method does NOT use FFS star detection — there are no individual
stars to find. It operates directly on residual flux in a window around
the reticle (= ``central_point`` = fiber position). ``uses_adu_match`` is
False; ``acquired`` tracks whether we have a useful flux signal above
threshold (i.e. star detectable on the detector at all).

"""

from __future__ import annotations

import logging
import warnings
from typing import Any

import numpy as np
from serverish.base import dt_from_array, dt_utcnow_array

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class FiberPhotocentroidMethod:
    """Photocentroid-based fiber guider.

    Args:
        fiber_radius_px: Geometric radius of the fiber hole on the
            detector (pixels). The dead-zone radius — apparent offsets
            below this produce no correction.
        analysis_radius_px: Half-size of the square window used for
            photocentroid analysis, centred on the reticle. Should be
            comfortably larger than ``fiber_radius_px`` (typically 3×)
            so the PSF wings are captured even when the star is well
            outside the hole.
        adu_sigma_threshold: How many sigmas above background the
            window's total excess flux must reach for the measurement
            to be considered valid (star detectable). Below the gate
            we return ``acquired=False`` and emit no correction —
            the star is either deep in the hole or simply not there.
        hole_zone_factor: Sets the upper boundary of the linear ramp
            at ``fiber_radius_px · hole_zone_factor``. Default 2.0 =
            ramp ends at twice the hole radius.
        saturation_adu: Pixels at or above this level are masked from
            the photocentroid sum. Saturated PSF cores are flat
            plateaus that, if included, drag the centroid toward the
            geometric centre of the saturated region — destroying
            sub-pixel resolution exactly where we need it.
    """

    name = "fiber_photocentroid"
    uses_adu_match = False
    produces_rotation = False

    def __init__(
        self,
        fiber_radius_px: float = 5.0,
        analysis_radius_px: float = 15.0,
        adu_sigma_threshold: float = 3.0,
        hole_zone_factor: float = 2.0,
        saturation_adu: float = 62_000.0,
        dead_zone_px: float | None = None,
        **params: Any,
    ) -> None:
        if hole_zone_factor <= 1.0:
            raise ValueError(
                f"hole_zone_factor must be > 1.0, got {hole_zone_factor!r} "
                "(needs strictly larger than 1 for the linear ramp to exist; "
                "use a value like 2.0)"
            )
        if dead_zone_px is not None and dead_zone_px < 0:
            raise ValueError(f"dead_zone_px must be >= 0, got {dead_zone_px!r}")
        self.fiber_radius_px = float(fiber_radius_px)
        self.analysis_radius_px = float(analysis_radius_px)
        self.adu_sigma_threshold = float(adu_sigma_threshold)
        self.hole_zone_factor = float(hole_zone_factor)
        self.saturation_adu = float(saturation_adu)
        # Dead-zone width decoupled from the hole radius. ``None``
        # selects the legacy behaviour (dead zone spans the full
        # ``fiber_radius_px``, then the ramp). When set, the piecewise
        # ramp is replaced by a plain threshold: below → zero, above →
        # 1:1 on the apparent offset, with the Enforcer's damping and
        # min-pulse policy handling overshoot and sub-threshold pulses.
        #
        # Why this exists: apparent offsets under-report nothing — the
        # photocentroid *over*-shoots (see module warning) — so a dead
        # zone equal to the hole radius (5 px apparent ≈ 3.4 px real)
        # silences the loop over offsets that already cost real
        # coupling. Stability of the 1:1 branch: with overshoot ≤ 2.5×
        # and damping 0.5 the per-cycle error ratio is ≤ |1 − 1.25| =
        # 0.25, i.e. convergent; the repetition guard and safety demote
        # bound the pathological cases. Runtime-reversible via a
        # ``method_params`` patch (no restart).
        self.dead_zone_px = None if dead_zone_px is None else float(dead_zone_px)
        self.params = params
        self.controller: Any | None = None

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        if frame.array.size == 0:
            return None

        # Phase classification — same as single_star: skip detection
        # while the mount is moving or settling. The post-settle frame
        # is the first one whose photocentroid reflects the new optical
        # position; preceding frames smear flux across the trajectory
        # and would compute a misleading centroid.
        active_pulse = state.get("active_pulse")
        exp_time = float(state.get("exp_time", 1.0)) or 1.0
        t_mid = None
        try:
            from datetime import timedelta  # noqa: PLC0415
            if isinstance(frame.timestamp, list) and len(frame.timestamp) >= 6:
                t_mid = dt_from_array(frame.timestamp) + timedelta(seconds=exp_time / 2.0)
        except Exception:  # noqa: BLE001
            t_mid = None
        from ocabox_tcs.services.guiding_svc.state import (  # noqa: PLC0415
            FramePhase, classify_frame_phase,
        )
        phase = classify_frame_phase(active_pulse, t_mid)

        central = _xy(state.get("central_point"))
        if central is None:
            return None

        if phase in (FramePhase.IN_FLIGHT, FramePhase.SETTLING):
            # Hold previous lock state and announce the phase so the UI
            # swaps overlays. Fiber mode keeps ``acquired_pos`` at the
            # last measured photocentroid position — handy for the UI
            # to show where the star was last seen.
            await self._notify_acquired(
                bool(state.get("acquired")),
                _xy(state.get("acquired_pos")),
                state.get("acquired_adu"),
                frame_phase=phase.value,
            )
            return None

        # Extract the analysis window. Window is a square of side
        # ``2·analysis_radius_px + 1`` centred at ``central_point``,
        # clipped to the frame bounds.
        H, W = frame.array.shape
        half = int(round(self.analysis_radius_px))
        cx_int = int(round(central[0]))
        cy_int = int(round(central[1]))
        x0, x1 = max(0, cx_int - half), min(W, cx_int + half + 1)
        y0, y1 = max(0, cy_int - half), min(H, cy_int + half + 1)
        if x1 - x0 < 3 or y1 - y0 < 3:
            # Window completely outside the frame — shouldn't happen in
            # practice but be defensive (e.g. operator dragged reticle
            # off-screen). Treat as "no signal".
            await self._notify_acquired(False, None, None, frame_phase=phase.value)
            return None
        window = frame.array[y0:y1, x0:x1].astype(np.float64)

        # Saturation mask — saturated pixels are excluded from every
        # statistic and contribute zero flux. See
        # SingleStar._subpixel_centroid for why a saturated PSF plateau
        # quantises the centroid to integer pixels.
        unsat = window < self.saturation_adu

        # Background removal by DE-STRIPING (per-row, then per-column
        # median subtraction) instead of a scalar edge median. The
        # guider sensor shows line-correlated readout structure
        # (horizontal banding, clearly visible on thumbnails); a smooth
        # illumination gradient is possible too. Both violate the
        # iid assumption behind the √n gate below: bands make whole
        # rows move together, so the effective number of independent
        # samples is ~rows, not pixels, and a scalar-background
        # residual sum passes the gate on pure background structure —
        # observed on hardware as a lock declared at ~40 ADU on a dark
        # region. Row/column medians remove bands and gradients while
        # leaving a compact star intact (the star occupies a minority
        # of any row/column of the window, so medians ignore it).
        # Saturated pixels are NaN during destriping so they can't
        # bias the medians; all-NaN lines produce NaN medians which
        # collapse to zero contribution below.
        work = np.where(unsat, window, np.nan)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN slices
            row_med = np.nanmedian(work, axis=1, keepdims=True)
            work = work - row_med
            col_med = np.nanmedian(work, axis=0, keepdims=True)
            work = work - col_med
        residual = np.nan_to_num(work, nan=0.0)
        bg = float(np.nanmedian(row_med)) if np.isfinite(row_med).any() else 0.0

        # Robust per-pixel noise from the MAD of the destriped residual.
        # After destriping the residual is zero-median by construction,
        # so the whole window (minus the star's minority of pixels,
        # which the median ignores) is a valid noise sample — no need
        # to restrict to the border.
        valid = residual[unsat]
        mad = float(np.median(np.abs(valid - np.median(valid)))) if valid.size else 0.0
        noise = 1.4826 * mad
        if noise <= 0:
            noise = float(np.std(valid)) if valid.size else 1.0
            noise = noise or 1.0

        net = np.clip(residual, 0.0, None)
        total_flux = float(net.sum())
        n_pix = int(unsat.sum())

        # ADU gate — computed on the UNCLIPPED destriped residual sum,
        # which is zero-mean under pure noise, so the standard "noise
        # grows like √N when summed over independent pixels" argument
        # holds and ``adu_sigma_threshold · noise · √n_pix`` is a valid
        # threshold.
        #
        # Two ways to get this wrong, both regression-locked in
        # ``tests/unit/test_fiber_photocentroid.py`` because both were
        # observed on sky as a lock onto empty background:
        # 1. The gate must NOT use ``total_flux`` (the clipped sum):
        #    clipping negatives makes pure noise sum to ≈ 0.4·n·σ vs a
        #    gate of 3σ√n, so empty sky passes 4× over threshold.
        # 2. The residual must be DE-STRIPED first (see above):
        #    line-correlated banding passes a scalar-background gate on
        #    structure alone.
        signal = float(residual.sum())
        gate = self.adu_sigma_threshold * noise * float(np.sqrt(max(n_pix, 1)))
        if signal < gate:
            # No usable signal — star is in the hole, behind a cloud,
            # or not there at all. Don't emit a correction; let the
            # mount track sidereally until flux returns.
            # INFO, not DEBUG: "no light reached the detector" and "light
            # is centred, nothing to do" are the two explanations for a
            # quiet mount, and both otherwise emit dx=dy=0 — the log is
            # the only place they can be told apart afterwards.
            logger.info(
                "fiber: NO SIGNAL — signal=%.0f < gate=%.0f (bg=%.1f noise=%.1f n=%d)",
                signal, gate, bg, noise, n_pix,
            )
            await self._notify_acquired(False, None, None, frame_phase=phase.value)
            return None

        # Photocentroid in window coordinates, then converted to offsets
        # relative to ``central_point``. ``x_offsets`` runs 0..(x1-x0-1)
        # along columns; subtract the integer reticle position within
        # the window to get offsets relative to ``central_point``
        # (ignoring the sub-pixel residual of central — operator placed
        # the reticle, the sub-px is below their granularity).
        yy, xx = np.indices(net.shape)
        # Offset of each pixel from central_point (cx_int, cy_int) in
        # the full sensor frame. (cx_int - x0) is the index of the
        # reticle pixel within the window.
        x_off = (xx - (cx_int - x0)).astype(np.float64)
        y_off = (yy - (cy_int - y0)).astype(np.float64)
        pc_x = float((net * x_off).sum() / total_flux)
        pc_y = float((net * y_off).sum() / total_flux)
        d_apparent = float(np.hypot(pc_x, pc_y))

        if self.dead_zone_px is not None:
            # Decoupled dead zone: plain threshold + 1:1. See __init__
            # for the rationale and the stability argument. The apparent
            # offset over-reports the real one (module warning), so the
            # 1:1 branch is an over-correction that the Enforcer's
            # damping turns into fast, convergent settling.
            d_corrected = 0.0 if d_apparent <= self.dead_zone_px else d_apparent
        else:
            # Legacy piecewise-linear ramp. Kept as the default until the
            # decoupled dead zone is validated on sky; see the module
            # warning for why its premise is inverted.
            fib_r = self.fiber_radius_px
            full_at = fib_r * self.hole_zone_factor
            if d_apparent <= fib_r:
                d_corrected = 0.0
            elif d_apparent >= full_at:
                d_corrected = d_apparent
            else:
                # Linear ramp from 0 at d=fib_r to full_at at d=full_at.
                t = (d_apparent - fib_r) / (full_at - fib_r)
                d_corrected = t * full_at

        # Scale the photocentroid vector to the corrected magnitude.
        #
        # Sign convention (see ``Correction`` docstring): ``dx_px`` is
        # the MEASURED ERROR, ``star − target`` — where the light is
        # relative to where it should be. The pulse-guide model's
        # ``predict()`` computes the cancelling pulse itself
        # (``motion = −error``, see ``pulse_guide.py``), so the solver
        # must NOT pre-negate. ``(pc_x, pc_y)`` already is
        # ``photocentroid − reticle`` = the error — pass it through.
        #
        # Do not "helpfully" negate here to push the star back: that
        # double-negates with the model and turns the loop into positive
        # feedback, driving the star away from the fibre (measured on sky:
        # error growing 0.3 → 12 px in ~11 s).
        if d_apparent > 1e-3 and d_corrected > 0:
            scale = d_corrected / d_apparent
            dx_corr = pc_x * scale
            dy_corr = pc_y * scale
        else:
            # Inside dead zone — emit a zero-magnitude correction so
            # the pipeline still produces a journal/event entry (operator
            # sees "we saw the star, it's centred, doing nothing"). The
            # Enforcer's ``min_pulse_ms`` filter will suppress the actual
            # pulse since the magnitude is 0.
            dx_corr = 0.0
            dy_corr = 0.0

        # Per-frame diagnostics at INFO. The ``metadata`` assembled below
        # is not published anywhere, so without this line the method's
        # decision is unobservable: a dead-zone verdict and a gate failure
        # both reach the mount as dx=dy=0. Volume is ~1 line per frame,
        # the same order as the controller and enforcer already emit.
        logger.info(
            "fiber: pc=(%+.2f,%+.2f) d_app=%.2f → d_corr=%.2f%s "
            "flux=%.0f gate=%.0f noise=%.1f n=%d",
            pc_x, pc_y, d_apparent, d_corrected,
            " [DEAD ZONE]" if d_corrected <= 0 else "",
            total_flux, gate, noise, n_pix,
        )

        # Lock-state update. ``acquired_pos`` = where the photocentroid
        # currently sits in the sensor frame, so the UI can plot a
        # marker. ``acquired_adu`` = mean ADU above background (a
        # rough signal-strength indicator for the UI gauge).
        photocentroid_pos = (
            float(central[0]) + pc_x,
            float(central[1]) + pc_y,
        )
        mean_adu_above_bg = total_flux / max(n_pix, 1)
        await self._notify_acquired(
            True, photocentroid_pos, mean_adu_above_bg,
            frame_phase=phase.value,
        )

        # Fiber mode corrects toward the fiber = ``central_point``. The
        # ``guide_anchor`` field is reserved for the single-star method's
        # "hold star where lock started" pattern; for fiber it's always
        # central_point because that's where the spectrograph entrance is.
        return Correction(
            dx_px=float(dx_corr),
            dy_px=float(dy_corr),
            method=self.name,
            confidence=_confidence(total_flux, gate),
            timestamp=dt_utcnow_array(),
            metadata={
                "phase": "fiber",
                "photocentroid_pos": list(photocentroid_pos),
                "pc_offset": [pc_x, pc_y],
                "d_apparent": d_apparent,
                "d_corrected": d_corrected,
                "total_flux": total_flux,
                "noise": noise,
                "gate": gate,
                "window": [int(x0), int(y0), int(x1), int(y1)],
                "n_pix": n_pix,
            },
        )

    async def _notify_acquired(
        self,
        acquired: bool,
        position: tuple[float, float] | None,
        adu: float | None,
        *,
        frame_phase: str | None = None,
    ) -> None:
        if self.controller is None:
            return
        try:
            await self.controller.notify_acquired(
                acquired=acquired, position=position, adu=adu,
                candidates=None,  # fiber mode has no per-frame star list
                recovery=False,
                frame_phase=frame_phase,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("controller.notify_acquired failed: %s", exc)

    def reset(self) -> None:
        # No internal state — every frame is independent.
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xy(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        return None


def _confidence(total_flux: float, gate: float) -> float:
    """How comfortably above the detection gate we are, clamped to [0, 1].

    1.0 = total_flux ≥ 2·gate (plenty of signal). 0.0 = just at gate
    (marginal). Useful for the UI to dim the photocentroid marker when
    confidence is low.
    """
    if gate <= 0:
        return 1.0
    ratio = (total_flux / gate - 1.0)  # 0 at gate, 1 at 2·gate
    return float(min(max(ratio, 0.0), 1.0))
