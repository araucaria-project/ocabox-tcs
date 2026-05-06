"""Selection policies — pick THE guide star from detected candidates.

Each policy is a callable taking `(coords, adu, state)` and returning
the index of the chosen detection (or `None` when no candidate
qualifies). `coords` is the array of star positions in `(x, y)`
convention (column, row); `adu` is the matching peak-ADU vector;
`state` is a snapshot dict from `PipelineStateHolder.snapshot()`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np


SelectionPolicy = Callable[
    [np.ndarray, np.ndarray, dict[str, Any]],
    int | None,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reference_point(state: dict[str, Any]) -> tuple[float, float]:
    """Pick the reference for distance comparisons.

    Prefers `acquired_pos` when locked, else falls back to
    `central_point`.
    """
    if state.get("acquired") and state.get("acquired_pos") is not None:
        ref = state["acquired_pos"]
    else:
        ref = state.get("central_point", (0.0, 0.0))
    return float(ref[0]), float(ref[1])


def _within_window(
    coords: np.ndarray, ref: tuple[float, float], half_size: float
) -> np.ndarray:
    """Boolean mask: detections inside a ±half_size square around ref."""
    if coords.size == 0:
        return np.zeros((0,), dtype=bool)
    dx = np.abs(coords[:, 0] - ref[0])
    dy = np.abs(coords[:, 1] - ref[1])
    return (dx <= half_size) & (dy <= half_size)


def _within_radius(
    coords: np.ndarray, ref: tuple[float, float], radius: float
) -> np.ndarray:
    """Boolean mask: detections inside a circle of `radius` around ref."""
    if coords.size == 0:
        return np.zeros((0,), dtype=bool)
    dx = coords[:, 0] - ref[0]
    dy = coords[:, 1] - ref[1]
    return (dx * dx + dy * dy) <= (radius * radius)


def _exclude_zones_mask(
    coords: np.ndarray, zones: Sequence[tuple[float, float, float]]
) -> np.ndarray:
    """Boolean mask: True for detections NOT inside any (x, y, r) zone."""
    if coords.size == 0:
        return np.zeros((0,), dtype=bool)
    keep = np.ones(coords.shape[0], dtype=bool)
    for zx, zy, zr in zones:
        dx = coords[:, 0] - zx
        dy = coords[:, 1] - zy
        keep &= (dx * dx + dy * dy) > (zr * zr)
    return keep


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


def _brightest_in_window(
    coords: np.ndarray, adu: np.ndarray, state: dict[str, Any]
) -> int | None:
    """First (ADU-descending) detection inside ±search_reg_px around ref.

    `coords` is assumed sorted ADU-descending (FFS default).
    """
    if coords.size == 0:
        return None
    ref = _reference_point(state)
    half = float(state.get("search_reg_px", 25))
    mask = _within_window(coords, ref, half)
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    return int(idxs[0])


def _brightest_in_adu_range(
    coords: np.ndarray, adu: np.ndarray, state: dict[str, Any]
) -> int | None:
    """First detection with `min_adu < adu < max_adu` (no position filter).

    Bounds are taken from `method_params['min_adu']` / `['max_adu']` if
    provided, else `(-inf, +inf)`.
    """
    if coords.size == 0:
        return None
    params = state.get("method_params", {}) or {}
    lo = float(params.get("min_adu", -np.inf))
    hi = float(params.get("max_adu", np.inf))
    mask = (adu > lo) & (adu < hi)
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    return int(idxs[0])


def _closest_in_window(
    coords: np.ndarray, adu: np.ndarray, state: dict[str, Any]
) -> int | None:
    """Closest detection (by distance) inside ±search_reg_px of ref."""
    if coords.size == 0:
        return None
    ref = _reference_point(state)
    half = float(state.get("search_reg_px", 25))
    mask = _within_window(coords, ref, half)
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    sub = coords[idxs]
    dx = sub[:, 0] - ref[0]
    dy = sub[:, 1] - ref[1]
    d2 = dx * dx + dy * dy
    return int(idxs[int(np.argmin(d2))])


def _closest_excluding_zone(
    coords: np.ndarray, adu: np.ndarray, state: dict[str, Any]
) -> int | None:
    """Closest detection inside the search window, excluding any
    detection that lies within `exclude_radius_px` of any pixel listed
    in `exclude_zones`.

    Inputs (from `state['method_params']`):
        exclude_zones: sequence of `(x, y, r)` tuples — explicit exclusion
            zones (each `r` is a per-zone radius in pixels). Default: `[]`.
        exclude_radius_px: float — extra radius added to every zone's `r`.
            Default: `0.0`.

    Reference point is `acquired_pos` if locked, else `central_point`.
    Search window is `±search_reg_px`. When acquired is False, the
    window may be widened to `±wide_search_radius_px` if the param is
    set; for now we honour `search_reg_px` to match other window-based
    policies and let the method widen via its own search box.
    """
    if coords.size == 0:
        return None
    params = state.get("method_params", {}) or {}
    raw_zones = params.get("exclude_zones", []) or []
    extra = float(params.get("exclude_radius_px", 0.0))
    zones: list[tuple[float, float, float]] = [
        (float(z[0]), float(z[1]), float(z[2]) + extra) for z in raw_zones
    ]

    keep = _exclude_zones_mask(coords, zones)
    if not np.any(keep):
        return None

    ref = _reference_point(state)
    half = float(state.get("search_reg_px", 25))
    in_window = _within_window(coords, ref, half)
    mask = keep & in_window
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    sub = coords[idxs]
    dx = sub[:, 0] - ref[0]
    dy = sub[:, 1] - ref[1]
    d2 = dx * dx + dy * dy
    return int(idxs[int(np.argmin(d2))])


def _weighted_score(
    coords: np.ndarray, adu: np.ndarray, state: dict[str, Any]
) -> int | None:
    """Rank by `adu / (1 + dist²)`; pick top score within search window."""
    if coords.size == 0:
        return None
    ref = _reference_point(state)
    half = float(state.get("search_reg_px", 25))
    mask = _within_window(coords, ref, half)
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    sub = coords[idxs]
    dx = sub[:, 0] - ref[0]
    dy = sub[:, 1] - ref[1]
    d2 = dx * dx + dy * dy
    score = adu[idxs] / (1.0 + d2)
    return int(idxs[int(np.argmax(score))])


SELECTION_POLICIES: dict[str, SelectionPolicy] = {
    "brightest_in_window": _brightest_in_window,
    "brightest_in_adu_range": _brightest_in_adu_range,
    "closest_in_window": _closest_in_window,
    "closest_excluding_zone": _closest_excluding_zone,
    "weighted_score": _weighted_score,
}
