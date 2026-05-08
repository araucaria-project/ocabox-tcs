"""SingleStarMethod — subraster crop + ADU-tolerance match.

Wide search on `acquired=False`, narrow re-acquisition on
`acquired=True`. Uses `pyaraucaria.ffs.FFS` for detection.

Coordinate convention: positions are `(x, y)` = (column, row). FFS
returns `(row, col)` natively; we swap at the boundary.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


def _ffs_detect(
    array: np.ndarray,
    threshold: float,
    fwhm: float,
    min_smoothed_sigma: float | None = None,
    rank_by: str = "smoothed",
    max_concentration: float | None = None,
    aperture_radius: int | None = None,
    min_pixels_above_threshold: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Run FFS detection and return `(coords_xy, adu)`.

    `coords_xy` is shape `(N, 2)` with columns `[x, y]`. FFS produces
    `(row, col)` natively; we swap to `(x, y)`.

    The two extra params are hot-pixel rejection knobs — see FFS docs:
    ``min_smoothed_sigma`` adds a kernel-matched SNR mask that drops
    isolated bright pixels; ``rank_by="smoothed"`` (our default —
    differs from FFS default) ranks survivors by their PSF-matched
    amplitude so a saturated hot pixel that *does* survive masks still
    falls below real stars.

    On any internal FFS failure we return empty arrays — Solver treats
    that as "no candidates this frame".
    """
    # pyaraucaria.ffs is an optional runtime dep; importing inside
    # keeps the module importable in test environments without it.
    from pyaraucaria.ffs import FFS

    if array.size == 0:
        return np.zeros((0, 2)), np.zeros((0,))

    ffs = FFS(image=array)
    try:
        ffs.mk_stats()
        ffs.find_stars(
            threshold=threshold,
            fwhm=fwhm,
            min_smoothed_sigma=min_smoothed_sigma,
            rank_by=rank_by,
            max_concentration=max_concentration,
            aperture_radius=aperture_radius,
            min_pixels_above_threshold=min_pixels_above_threshold,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("FFS detection failed: %s", exc)
        return np.zeros((0, 2)), np.zeros((0,))

    coo = ffs.coo
    adu = ffs.adu
    if coo is None or len(coo) == 0:
        return np.zeros((0, 2)), np.zeros((0,))

    coo = np.asarray(coo, dtype=float)
    adu = np.asarray(adu, dtype=float)
    # FFS gives (row, col); swap to (x=col, y=row).
    coords_xy = np.column_stack([coo[:, 1], coo[:, 0]])
    return coords_xy, adu


class SingleStarMethod:
    """Single-star guider using subraster + ADU-tolerance matching.

    Wide search (`acquired=False`): detect inside a circle of radius
    `wide_search_radius_px` around `central_point`; pick brightest;
    promote to acquired.

    Narrow re-acquisition (`acquired=True`): detect inside a square of
    half-size `search_reg_px` around `acquired_pos`; accept the
    candidate whose ADU is within `tolerance = adu_match_tolerance_per_sec
    * exp_time` of `acquired_adu` and is closest to `acquired_pos`. If
    nothing matches, demote to `acquired=False` and return `None`.

    Args:
        fwhm: PSF FWHM (pixels) used by FFS for detection smoothing.
        threshold: FFS detection threshold (sigma).
        saturation_adu: Used to derive `confidence` as ADU/saturation.
        min_smoothed_sigma: Kernel-matched SNR threshold applied to
            FFS's Gaussian-smoothed image. Hot pixels & narrow noise
            spikes collapse under the convolution and fail this test;
            real PSF-shaped sources stay bright. ``None`` disables
            (FFS default behaviour). Recommended ~3.0 for typical
            guiders that suffer from saturated hot pixels.
        rank_by: ``"smoothed"`` ranks candidates by kernel-matched
            amplitude so real stars outrank surviving hot pixels even
            without strict masking; ``"raw"`` is the FFS default
            (peak ADU, hot pixels win).
    """

    name = "single_star"
    uses_adu_match = True
    produces_rotation = False

    def __init__(
        self,
        fwhm: float = 3.0,
        threshold: float = 5.0,
        saturation_adu: float = 50_000.0,
        min_smoothed_sigma: float | None = None,
        rank_by: str = "smoothed",
        max_concentration: float | None = None,
        aperture_radius: int | None = None,
        min_pixels_above_threshold: int | None = None,
        centroid_radius_px: int | None = None,
        centroid_threshold_sigma: float | None = None,
        **params: Any,
    ) -> None:
        self.fwhm = float(fwhm)
        self.threshold = float(threshold)
        self.saturation_adu = float(saturation_adu)
        self.min_smoothed_sigma = (
            float(min_smoothed_sigma) if min_smoothed_sigma is not None else None
        )
        self.max_concentration = (
            float(max_concentration) if max_concentration is not None else None
        )
        self.aperture_radius = (
            int(aperture_radius) if aperture_radius is not None else None
        )
        self.min_pixels_above_threshold = (
            int(min_pixels_above_threshold)
            if min_pixels_above_threshold is not None else None
        )
        # Centroid box radius — defaults to ~FWHM. Bigger captures
        # multi-detection clusters (fiber halo, reflections) so the
        # blob-centroid is stable when the detector picks different
        # peaks from the same cluster across frames.
        self.centroid_radius_px = (
            int(centroid_radius_px) if centroid_radius_px is not None else None
        )
        # Centroid pixel-selection threshold (in σ above local sky).
        # When set, only above-threshold pixels contribute — turns the
        # plain flux-weighted centroid into a "blob centroid" that's
        # robust to multi-peak clusters.
        self.centroid_threshold_sigma = (
            float(centroid_threshold_sigma)
            if centroid_threshold_sigma is not None else None
        )
        self.rank_by = rank_by
        self.params = params
        # Optional controller hook for lock-state notifications.
        # Wired by the Solver at runtime; None on the sim/dev path.
        self.controller: Any | None = None
        # Stick-with-it: count consecutive narrow-search misses while
        # we already have a lock. Frames can be transiently bad — second
        # observer hammering the same camera, network glitch, brief
        # cloud — and the right reaction is to skip them, not declare
        # the star lost. Only after ``_narrow_miss_threshold`` consecutive
        # misses do we demote to wide-search recovery. Resets to zero on
        # any successful re-detection.
        self._narrow_miss_count = 0
        self._narrow_miss_threshold = 5

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        if frame.array.size == 0:
            return None

        # Detection is always full-frame, regardless of mode. Two reasons:
        #   1. The UI overlay must show *fresh* candidates every frame —
        #      otherwise stale circles linger after lock_at and obscure
        #      what the solver is actually seeing.
        #   2. Background statistics are stable on the full frame; on a
        #      tiny narrow-search crop they're noisy enough that real
        #      stars sometimes fall below the detection threshold.
        # Cost: ~70 ms / frame on the BESO sensor. Comfortably under the
        # exposure cadence; worth it for the consistency.
        coords, adu = _ffs_detect(
            frame.array,
            self.threshold,
            self.fwhm,
            min_smoothed_sigma=self.min_smoothed_sigma,
            rank_by=self.rank_by,
            max_concentration=self.max_concentration,
            aperture_radius=self.aperture_radius,
            min_pixels_above_threshold=self.min_pixels_above_threshold,
        )
        candidates_full = _candidates_payload(coords, adu)

        if state.get("acquired"):
            return await self._narrow(frame, state, coords, adu, candidates_full)
        return await self._wide(frame, state, coords, adu, candidates_full)

    # ------------------------------------------------------------------
    # Wide search (cold start / post-loss recovery)
    # ------------------------------------------------------------------

    async def _wide(
        self,
        frame: AnalysisFrame,
        state: dict[str, Any],
        coords: np.ndarray,
        adu: np.ndarray,
        candidates_full: list[tuple[float, float, float]],
    ) -> Correction | None:
        central = _xy(state.get("central_point"))
        if central is None:
            logger.debug("wide search: no central_point in state")
            return None
        radius = float(state.get("wide_search_radius_px", 200))

        if coords.shape[0] == 0:
            await self._notify_acquired(False, None, None, candidates=candidates_full)
            return None

        # Filter by circular wide-search region.
        dx = coords[:, 0] - central[0]
        dy = coords[:, 1] - central[1]
        in_range = (dx * dx + dy * dy) <= (radius * radius)
        idxs = np.flatnonzero(in_range)
        if idxs.size == 0:
            await self._notify_acquired(False, None, None, candidates=candidates_full)
            return None

        # Smart relock — favour the same physical star we held before.
        #
        # Default selection (cold start, no prior lock) ranks by FFS's
        # own brightness ordering = closest-to-rank-1 inside the wide
        # circle. Once we *had* a lock and lost it (drop pulse moved
        # the star out of the narrow box, brief seeing dip, etc.), we
        # have an excellent prior: the star didn't teleport, and its
        # ADU shouldn't change much frame to frame. So we score
        # candidates by combined position-proximity + ADU-similarity
        # to the last good detection — that picks the same physical
        # star even when it lands closer to ``central_point`` than to
        # its previous spot, or when there's a brighter neighbour
        # nearby that the cold-start ranker would prefer.
        last_pos = _xy(state.get("last_acquired_pos"))
        last_adu_v = state.get("last_acquired_adu")
        last_adu = float(last_adu_v) if last_adu_v is not None else None

        if last_pos is not None:
            # Score: proximity² normalised by wide_search radius² +
            # |Δadu|/last_adu (relative ADU error). Position weight
            # 0.7 dominates — lock loss happens in a small window so
            # the same star is usually within a few px even after a
            # capped pulse. ADU is a tiebreaker between similar
            # brightness candidates at similar distance.
            cand = coords[idxs]
            cand_adu = adu[idxs]
            ddx = cand[:, 0] - last_pos[0]
            ddy = cand[:, 1] - last_pos[1]
            d2 = ddx * ddx + ddy * ddy
            pos_score = d2 / (radius * radius)
            if last_adu is not None and last_adu > 1e-3:
                adu_err = np.abs(cand_adu - last_adu) / last_adu
                # Cap relative error at 1.0 so a 10× brighter spurious
                # candidate doesn't dominate the score.
                adu_score = np.minimum(adu_err, 1.0)
                score = 0.7 * pos_score + 0.3 * adu_score
            else:
                score = pos_score
            chosen_local = int(np.argmin(score))
            chosen = int(idxs[chosen_local])
        else:
            # Cold start — use FFS's brightness ranking (best within window).
            chosen = int(idxs[0])
        peak_x = float(coords[chosen, 0])
        peak_y = float(coords[chosen, 1])
        chosen_adu = float(adu[chosen])
        # Subpixel refinement — FFS returns integer pixel positions;
        # the actual centroid is flux-weighted within ~one PSF FWHM.
        sub_x, sub_y = _subpixel_centroid(
            frame.array, peak_x, peak_y,
            half=self.centroid_radius_px or max(2, int(round(self.fwhm))),
            threshold_sigma=self.centroid_threshold_sigma,
            saturation_adu=self.saturation_adu,
        )
        pos = (sub_x, sub_y)

        # Promote to acquired. ``recovery=True`` when we had a previous
        # lock fingerprint — the controller treats this as a real
        # post-loss recovery and resets ``guide_anchor`` in guiding
        # mode (safe-failure rule, see Controller.notify_acquired
        # docstring). Cold start (``last_pos is None``) → recovery=False
        # so the first lock of the session keeps the operator's chosen
        # ``central_point`` as the implicit anchor target.
        await self._notify_acquired(
            True, pos, chosen_adu,
            candidates=candidates_full,
            recovery=last_pos is not None,
        )

        # Correction reference: ``guide_anchor`` (where guiding holds
        # the star) wins over ``central_point`` (operator's target
        # reticle). Wide-search runs in monitoring/cold-start when
        # ``guide_anchor`` is None — fall back to ``central_point`` so
        # the very first lock during a guiding session lands at the
        # operator's chosen target before the anchor is captured.
        anchor = _xy(state.get("guide_anchor")) or central
        dx_px = pos[0] - anchor[0]
        dy_px = pos[1] - anchor[1]
        return Correction(
            dx_px=float(dx_px),
            dy_px=float(dy_px),
            method=self.name,
            confidence=_confidence(chosen_adu, self.saturation_adu),
            timestamp=dt_utcnow_array(),
            metadata={
                "phase": "wide_search",
                "star_pos": list(pos),
                "star_adu": chosen_adu,
                "n_candidates": int(coords.shape[0]),
                "n_in_range": int(idxs.size),
            },
        )

    # ------------------------------------------------------------------
    # Narrow re-acquisition
    # ------------------------------------------------------------------

    async def _narrow(
        self,
        frame: AnalysisFrame,
        state: dict[str, Any],
        coords: np.ndarray,
        adu: np.ndarray,
        candidates_full: list[tuple[float, float, float]],
    ) -> Correction | None:
        acquired_pos = _xy(state.get("acquired_pos"))
        if acquired_pos is None:
            # Inconsistent state: acquired=True but no position. Demote
            # immediately — this isn't a transient detection miss but a
            # state-machine glitch that wide search can recover from.
            self._narrow_miss_count = 0
            await self._notify_acquired(False, None, None, candidates=candidates_full)
            return None
        acquired_adu = state.get("acquired_adu")
        central = _xy(state.get("central_point")) or acquired_pos
        half = float(state.get("search_reg_px", 25))
        exp_time = float(state.get("exp_time", 1.0))
        tol_per_sec = state.get("adu_match_tolerance_per_sec")
        tolerance: float | None = (
            float(tol_per_sec) * exp_time if tol_per_sec is not None else None
        )

        if coords.shape[0] == 0:
            return await self._handle_narrow_miss(
                "no detections in full frame", candidates_full,
                hold_pos=acquired_pos, hold_adu=acquired_adu,
            )

        # Spatial filter: keep candidates in the search box around the
        # last lock. Same logic as the prior crop-detection — but
        # operating on full-frame candidates so we get consistent
        # background statistics and a fresh overlay every frame.
        in_box = (
            (np.abs(coords[:, 0] - acquired_pos[0]) <= half)
            & (np.abs(coords[:, 1] - acquired_pos[1]) <= half)
        )
        idxs_box = np.flatnonzero(in_box)
        if idxs_box.size == 0:
            return await self._handle_narrow_miss(
                "no candidate in search box", candidates_full,
                hold_pos=acquired_pos, hold_adu=acquired_adu,
            )

        # ADU tolerance filter (relative to *acquired_adu*).
        if tolerance is not None and acquired_adu is not None:
            adu_lo = float(acquired_adu) - tolerance
            adu_hi = float(acquired_adu) + tolerance
            in_tol = (adu[idxs_box] >= adu_lo) & (adu[idxs_box] <= adu_hi)
            idxs = idxs_box[in_tol]
        else:
            idxs = idxs_box

        if idxs.size == 0:
            return await self._handle_narrow_miss(
                "no candidate matches ADU tolerance", candidates_full,
                hold_pos=acquired_pos, hold_adu=acquired_adu,
            )

        # Pick the candidate closest to the previous acquired_pos.
        sub = coords[idxs]
        ddx = sub[:, 0] - acquired_pos[0]
        ddy = sub[:, 1] - acquired_pos[1]
        d2 = ddx * ddx + ddy * ddy
        chosen = int(idxs[int(np.argmin(d2))])

        peak_x = float(coords[chosen, 0])
        peak_y = float(coords[chosen, 1])
        new_adu = float(adu[chosen])
        sub_x, sub_y = _subpixel_centroid(
            frame.array, peak_x, peak_y,
            half=self.centroid_radius_px or max(2, int(round(self.fwhm))),
            threshold_sigma=self.centroid_threshold_sigma,
            saturation_adu=self.saturation_adu,
        )
        new_pos = (sub_x, sub_y)

        # Update lock position. Successful re-detection resets the
        # consecutive-miss counter — we tolerated a few bad frames and
        # the star came back.
        self._narrow_miss_count = 0
        await self._notify_acquired(True, new_pos, new_adu, candidates=candidates_full)

        # Correction: the requested pixel offset to bring the star back
        # to the guide anchor (= where lock was when guiding started),
        # falling back to ``central_point`` outside guiding. See _wide
        # for the rationale on the anchor-vs-central split.
        anchor = _xy(state.get("guide_anchor")) or central
        dx_px = new_pos[0] - anchor[0]
        dy_px = new_pos[1] - anchor[1]
        return Correction(
            dx_px=float(dx_px),
            dy_px=float(dy_px),
            method=self.name,
            confidence=_confidence(new_adu, self.saturation_adu),
            timestamp=dt_utcnow_array(),
            metadata={
                "phase": "narrow",
                "star_pos": list(new_pos),
                "star_adu": new_adu,
                "n_candidates": int(coords.shape[0]),
                "n_in_box": int(idxs_box.size),
                "n_in_adu_range": int(idxs.size),
                "tolerance": tolerance,
                "prev_pos": list(acquired_pos),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _notify_acquired(
        self,
        acquired: bool,
        position: tuple[float, float] | None,
        adu: float | None,
        *,
        candidates: list[tuple[float, float, float]] | None = None,
        recovery: bool = False,
    ) -> None:
        if self.controller is None:
            return
        try:
            await self.controller.notify_acquired(
                acquired=acquired, position=position, adu=adu,
                candidates=candidates, recovery=recovery,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("controller.notify_acquired failed: %s", exc)

    async def _handle_narrow_miss(
        self,
        reason: str,
        candidates_full: list[tuple[float, float, float]],
        *,
        hold_pos: tuple[float, float] | None = None,
        hold_adu: float | None = None,
    ) -> None:
        """Tolerate transient narrow-search misses without dropping the
        lock. Increment a consecutive-miss counter and only demote
        ``acquired`` after ``_narrow_miss_threshold`` frames in a row.

        Use cases:
        - second observer hammering the same Alpaca camera causes
          intermittent stale frames where detection legitimately fails
        - brief seeing dip drops a faint star below threshold for one
          frame
        - cosmic ray vs detection threshold edge case

        On success ``_narrow_miss_count`` resets to zero, so the
        threshold is consecutive — we tolerate up to N bad frames in a
        row but a single good one resets the budget.

        Crucially: while we're holding the lock through misses, we DON'T
        publish any state change (still ``acquired=True``, same
        position) and we don't emit a correction this frame. The
        Enforcer's pulse-cooldown means a missed frame in guiding mode
        just means one less correction — the next good frame resumes.
        """
        self._narrow_miss_count += 1
        if self._narrow_miss_count <= self._narrow_miss_threshold:
            logger.debug(
                "narrow miss %d/%d: %s — keeping lock",
                self._narrow_miss_count, self._narrow_miss_threshold, reason,
            )
            # Republish lock state with current acquired_pos preserved
            # AND fresh candidates so the UI overlay reflects what
            # detection saw this frame — operator can see *why* the
            # narrow box came up empty (detections present but outside
            # the box, vs no detections at all).
            await self._notify_acquired(
                True, hold_pos, hold_adu, candidates=candidates_full,
            )
            return None
        # Exceeded budget — declare lost so wide-search can recover.
        logger.info(
            "narrow miss budget exceeded (%d frames): %s — demoting to wide",
            self._narrow_miss_count, reason,
        )
        self._narrow_miss_count = 0
        await self._notify_acquired(False, None, None, candidates=candidates_full)
        return None

    def reset(self) -> None:
        # No internal state to clear — the lock lives in PipelineState.
        self._narrow_miss_count = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xy(value: Any) -> tuple[float, float] | None:
    """Coerce a 2-tuple/list into `(x, y)` floats; `None` passes through."""
    if value is None:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError):
        return None


def _confidence(adu: float, saturation: float) -> float:
    """Quick-and-cheap quality score: ADU / saturation, clamped to [0, 1]."""
    if saturation <= 0:
        return 1.0
    return float(min(max(adu / saturation, 0.0), 1.0))


def _subpixel_centroid(
    image: np.ndarray,
    x: float,
    y: float,
    half: int,
    threshold_sigma: float | None = None,
    saturation_adu: float | None = None,
) -> tuple[float, float]:
    """Flux-weighted centroid in a ``(2·half+1)²`` box around (x, y).

    Three masking decisions, in order:

    1. **Saturation mask** — if ``saturation_adu`` is given, pixels at or
       above that level are excluded from the centroid weight entirely.
       The reason is critical for sub-pixel accuracy on bright stars:
       a saturated PSF core is a *plateau* of identical clipped values,
       and the centroid of a constant-value plateau is its geometric
       centre — i.e. the integer pixel at the centre of the saturated
       blob, *destroying sub-pixel information*. The actual sub-pixel
       position lives in the un-saturated **PSF wings** where the
       gradient is. Excluding the plateau forces the centroid to be
       computed from the wings, where it can resolve fractional pixels
       again. Without this, a saturated 5×5 plateau gives integer
       centroids and the operator sees discrete "grid" artefacts on
       the drift chart.

    2. **Mode masking** (after saturation):
       a. ``threshold_sigma=None`` → plain centroid: all (un-saturated)
          pixels, background-subtracted and clipped ≥ 0, contribute
          weighted by excess. Best for clean isolated PSFs without
          multi-peak structure.
       b. ``threshold_sigma`` set, e.g. 3 → blob centroid: only pixels
          exceeding ``edge_median + threshold_sigma · edge_MAD_sigma``
          contribute. Robust to multi-peak clusters where the detector
          might pick any of several nearby peaks (fibre halos,
          reflections, hot-pixel families). The same set of wing
          pixels survives across detector jumps, so the returned
          centroid is stable.

    3. **Fallback**: if the patch is degenerate (< 3 px) or no pixels
       survive masking, return the integer input — the caller's lock
       position is preserved rather than corrupted by a bad sample.

    Args:
        image: Full sensor frame.
        x, y: Approximate centre pixel (typically FFS detection peak).
        half: Box half-width in pixels.
        threshold_sigma: Blob-mode threshold (None → plain centroid).
        saturation_adu: Pixel value at/above which a pixel is counted
            as saturated and excluded. Pass the camera's saturation
            level (often ~65500 for uint16 cameras). None → no
            saturation handling (centroid will be integer-quantised on
            saturated PSFs — only safe when you know stars are below
            saturation).

    Returns ``(sub_x, sub_y)`` in full-frame sensor coords.
    """
    H, W = image.shape
    cx_int, cy_int = int(round(x)), int(round(y))
    x0, x1 = max(0, cx_int - half), min(W, cx_int + half + 1)
    y0, y1 = max(0, cy_int - half), min(H, cy_int + half + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return float(x), float(y)
    patch = image[y0:y1, x0:x1].astype(np.float64)
    edge = np.concatenate([
        patch[0, :].ravel(), patch[-1, :].ravel(),
        patch[1:-1, 0].ravel(), patch[1:-1, -1].ravel(),
    ])
    bg = float(np.median(edge))

    # Saturation mask — built once, applied to whichever weighting
    # branch fires below. ``< saturation_adu`` keeps everything strictly
    # below the clip; the plateau pixels get zero weight.
    if saturation_adu is not None and saturation_adu > 0:
        unsat = patch < float(saturation_adu)
    else:
        unsat = np.ones_like(patch, dtype=bool)

    if threshold_sigma is not None:
        # Robust local sigma from edge pixels (MAD-based; falls back to
        # std() if MAD collapses on a near-uniform edge).
        edge_mad = float(np.median(np.abs(edge - bg)))
        local_sigma = 1.4826 * edge_mad
        if local_sigma <= 0:
            local_sigma = float(np.std(edge)) or 1.0
        threshold = bg + float(threshold_sigma) * local_sigma
        # Only above-threshold AND below-saturation pixels contribute.
        mask = (patch > threshold) & unsat
        work = np.where(mask, patch - bg, 0.0)
    else:
        work = np.where(unsat, np.clip(patch - bg, 0.0, None), 0.0)

    total = float(work.sum())
    if total <= 0.0:
        # All pixels saturated, or none above threshold — preserve the
        # caller's integer estimate rather than producing a NaN.
        return float(x), float(y)
    yy, xx = np.indices(work.shape)
    sub_x = float((xx * work).sum() / total + x0)
    sub_y = float((yy * work).sum() / total + y0)
    return sub_x, sub_y


def _candidates_payload(
    coords: np.ndarray,
    adu: np.ndarray,
    top_n: int = 200,
) -> list[tuple[float, float, float]]:
    """Pack FFS detection output into a JSON-friendly ranked list.

    Returned shape: ``[(x, y, adu), …]`` already in rank order (FFS
    sorts before returning). Capped at ``top_n`` so the per-frame
    state publish stays bounded — 200 × 24 bytes ≈ 5 KB.
    """
    n = min(int(top_n), int(coords.shape[0]))
    if n == 0:
        return []
    return [
        (float(coords[i, 0]), float(coords[i, 1]), float(adu[i]))
        for i in range(n)
    ]
