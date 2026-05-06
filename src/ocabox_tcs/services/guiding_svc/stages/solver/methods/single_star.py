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
    array: np.ndarray, threshold: float, fwhm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Run FFS detection and return `(coords_xy, adu)`.

    `coords_xy` is shape `(N, 2)` with columns `[x, y]`. FFS produces
    `(row, col)` natively; we swap to `(x, y)`.

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
        ffs.find_stars(threshold=threshold, fwhm=fwhm)
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
    """

    name = "single_star"
    uses_adu_match = True
    produces_rotation = False

    def __init__(
        self,
        fwhm: float = 3.0,
        threshold: float = 5.0,
        saturation_adu: float = 50_000.0,
        **params: Any,
    ) -> None:
        self.fwhm = float(fwhm)
        self.threshold = float(threshold)
        self.saturation_adu = float(saturation_adu)
        self.params = params
        # Optional controller hook for lock-state notifications.
        # Wired by the Solver at runtime; None on the sim/dev path.
        self.controller: Any | None = None

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        if frame.array.size == 0:
            return None

        if state.get("acquired"):
            return await self._narrow(frame, state)
        return await self._wide(frame, state)

    # ------------------------------------------------------------------
    # Wide search (cold start / post-loss recovery)
    # ------------------------------------------------------------------

    async def _wide(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        central = _xy(state.get("central_point"))
        if central is None:
            logger.debug("wide search: no central_point in state")
            return None
        radius = float(state.get("wide_search_radius_px", 200))

        coords, adu = _ffs_detect(frame.array, self.threshold, self.fwhm)
        if coords.shape[0] == 0:
            return None

        # Filter by circular wide-search region.
        dx = coords[:, 0] - central[0]
        dy = coords[:, 1] - central[1]
        in_range = (dx * dx + dy * dy) <= (radius * radius)
        idxs = np.flatnonzero(in_range)
        if idxs.size == 0:
            return None

        # FFS already sorts ADU-descending — first index in `idxs` is
        # brightest within the window.
        chosen = int(idxs[0])
        pos = (float(coords[chosen, 0]), float(coords[chosen, 1]))
        chosen_adu = float(adu[chosen])

        # Promote to acquired (controller is the authoritative writer;
        # if it isn't wired, we still emit the correction — useful for
        # sim/dev runs).
        await self._notify_acquired(True, pos, chosen_adu)

        dx_px = pos[0] - central[0]
        dy_px = pos[1] - central[1]
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
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        acquired_pos = _xy(state.get("acquired_pos"))
        if acquired_pos is None:
            # Inconsistent state: acquired=True but no position.
            # Demote and trigger wide search next frame.
            await self._notify_acquired(False, None, None)
            return None
        acquired_adu = state.get("acquired_adu")
        central = _xy(state.get("central_point")) or acquired_pos
        half = float(state.get("search_reg_px", 25))
        exp_time = float(state.get("exp_time", 1.0))
        tol_per_sec = state.get("adu_match_tolerance_per_sec")
        tolerance: float | None = (
            float(tol_per_sec) * exp_time if tol_per_sec is not None else None
        )

        # Crop subraster around acquired_pos. Coords back to row/col for
        # numpy slicing: row = y, col = x.
        h, w = frame.array.shape[:2]
        x_min = max(0, int(acquired_pos[0] - half))
        x_max = min(w, int(acquired_pos[0] + half))
        y_min = max(0, int(acquired_pos[1] - half))
        y_max = min(h, int(acquired_pos[1] + half))
        if x_max <= x_min or y_max <= y_min:
            await self._notify_acquired(False, None, None)
            return None

        crop = frame.array[y_min:y_max, x_min:x_max]
        coords_local, adu = _ffs_detect(crop, self.threshold, self.fwhm)
        if coords_local.shape[0] == 0:
            await self._notify_acquired(False, None, None)
            return None

        # Lift detection coords back into the full-frame.
        coords = coords_local + np.array([x_min, y_min], dtype=float)

        # ADU tolerance filter (relative to *acquired_adu*).
        if tolerance is not None and acquired_adu is not None:
            adu_lo = float(acquired_adu) - tolerance
            adu_hi = float(acquired_adu) + tolerance
            adu_mask = (adu >= adu_lo) & (adu <= adu_hi)
        else:
            adu_mask = np.ones(adu.shape[0], dtype=bool)

        idxs = np.flatnonzero(adu_mask)
        if idxs.size == 0:
            await self._notify_acquired(False, None, None)
            return None

        # Pick the candidate closest to the previous acquired_pos.
        sub = coords[idxs]
        ddx = sub[:, 0] - acquired_pos[0]
        ddy = sub[:, 1] - acquired_pos[1]
        d2 = ddx * ddx + ddy * ddy
        chosen = int(idxs[int(np.argmin(d2))])

        new_pos = (float(coords[chosen, 0]), float(coords[chosen, 1]))
        new_adu = float(adu[chosen])

        # Update lock position.
        await self._notify_acquired(True, new_pos, new_adu)

        # Correction: the requested pixel offset to bring the star back
        # to the central_point reference (architecture §3.4).
        dx_px = new_pos[0] - central[0]
        dy_px = new_pos[1] - central[1]
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
                "n_candidates": int(coords_local.shape[0]),
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
    ) -> None:
        if self.controller is None:
            return
        try:
            await self.controller.notify_acquired(
                acquired=acquired, position=position, adu=adu
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("controller.notify_acquired failed: %s", exc)

    def reset(self) -> None:
        # No internal state to clear — the lock lives in PipelineState.
        pass


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
