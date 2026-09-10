"""Unit tests for solver selection policies.

Pure-numeric tests: no FFS, no AnalysisFrame; we hand the policy a
synthetic `(coords, adu)` pair and a state snapshot dict.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ocabox_tcs.services.guiding_svc.stages.solver.selection_policies import (
    SELECTION_POLICIES,
)


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "central_point": (100.0, 100.0),
        "search_reg_px": 25,
        "acquired": False,
        "acquired_pos": None,
        "method_params": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# brightest_in_window
# ---------------------------------------------------------------------------


def test_brightest_in_window_first_in_adu_order() -> None:
    policy = SELECTION_POLICIES["brightest_in_window"]
    # Coords sorted ADU-descending (FFS convention).
    coords = np.array([[105.0, 95.0], [80.0, 80.0], [110.0, 110.0]])
    adu = np.array([10_000.0, 5_000.0, 2_000.0])
    state = _state()
    idx = policy(coords, adu, state)
    # First (brightest) is in window of (100,100) ±25.
    assert idx == 0


def test_brightest_in_window_skips_outside() -> None:
    policy = SELECTION_POLICIES["brightest_in_window"]
    # Brightest is outside ±25 window; second one is inside.
    coords = np.array([[1.0, 1.0], [105.0, 95.0]])
    adu = np.array([10_000.0, 5_000.0])
    idx = policy(coords, adu, _state())
    assert idx == 1


def test_brightest_in_window_returns_none_when_empty() -> None:
    policy = SELECTION_POLICIES["brightest_in_window"]
    idx = policy(np.zeros((0, 2)), np.zeros((0,)), _state())
    assert idx is None


# ---------------------------------------------------------------------------
# closest_in_window
# ---------------------------------------------------------------------------


def test_closest_in_window_picks_closest_to_central() -> None:
    policy = SELECTION_POLICIES["closest_in_window"]
    coords = np.array([[110.0, 110.0], [102.0, 99.0], [120.0, 80.0]])
    adu = np.array([10_000.0, 1_000.0, 8_000.0])
    state = _state()  # central=(100,100), reg_px=25
    idx = policy(coords, adu, state)
    # (102,99) is closest to (100,100).
    assert idx == 1


def test_closest_in_window_uses_acquired_pos_when_locked() -> None:
    policy = SELECTION_POLICIES["closest_in_window"]
    coords = np.array([[100.0, 100.0], [200.0, 200.0]])
    adu = np.array([10_000.0, 1_000.0])
    state = _state(
        acquired=True, acquired_pos=(200.0, 200.0), search_reg_px=25,
        central_point=(0.0, 0.0),
    )
    idx = policy(coords, adu, state)
    # Reference is (200, 200); (200, 200) is closest and in window.
    assert idx == 1


# ---------------------------------------------------------------------------
# closest_excluding_zone
# ---------------------------------------------------------------------------


def test_closest_excluding_zone_no_zones_picks_closest() -> None:
    policy = SELECTION_POLICIES["closest_excluding_zone"]
    coords = np.array([[110.0, 110.0], [102.0, 99.0]])
    adu = np.array([5_000.0, 5_000.0])
    state = _state()  # method_params has no exclude_zones
    idx = policy(coords, adu, state)
    assert idx == 1


def test_closest_excluding_zone_excludes_inside_zone() -> None:
    policy = SELECTION_POLICIES["closest_excluding_zone"]
    # Closest is inside an exclude zone -> falls back to next closest.
    coords = np.array([[101.0, 101.0], [110.0, 110.0]])
    adu = np.array([5_000.0, 5_000.0])
    state = _state(
        method_params={"exclude_zones": [(100.0, 100.0, 5.0)]},
    )
    idx = policy(coords, adu, state)
    # (101,101) is within radius 5 of (100,100); rejected.
    assert idx == 1


def test_closest_excluding_zone_radius_param_widens_zones() -> None:
    policy = SELECTION_POLICIES["closest_excluding_zone"]
    coords = np.array([[103.0, 103.0], [115.0, 115.0]])
    adu = np.array([5_000.0, 5_000.0])
    # Zone radius 1, but param adds extra 10 -> effective radius 11.
    state = _state(
        method_params={
            "exclude_zones": [(100.0, 100.0, 1.0)],
            "exclude_radius_px": 10.0,
        },
    )
    idx = policy(coords, adu, state)
    # (103,103) is within sqrt(18) ≈ 4.24 of (100,100), inside extended zone.
    assert idx == 1


def test_closest_excluding_zone_all_excluded_returns_none() -> None:
    policy = SELECTION_POLICIES["closest_excluding_zone"]
    coords = np.array([[100.0, 100.0], [101.0, 101.0]])
    adu = np.array([5_000.0, 5_000.0])
    state = _state(
        method_params={"exclude_zones": [(100.0, 100.0, 50.0)]},
    )
    idx = policy(coords, adu, state)
    assert idx is None


def test_closest_excluding_zone_empty_returns_none() -> None:
    policy = SELECTION_POLICIES["closest_excluding_zone"]
    idx = policy(np.zeros((0, 2)), np.zeros((0,)), _state())
    assert idx is None


# ---------------------------------------------------------------------------
# brightest_in_adu_range
# ---------------------------------------------------------------------------


def test_brightest_in_adu_range_filters_by_bounds() -> None:
    policy = SELECTION_POLICIES["brightest_in_adu_range"]
    coords = np.array([[10.0, 10.0], [20.0, 20.0], [30.0, 30.0]])
    adu = np.array([100_000.0, 30_000.0, 1_000.0])
    state = _state(method_params={"min_adu": 10_000, "max_adu": 50_000})
    idx = policy(coords, adu, state)
    # Only the second one is in [10k, 50k].
    assert idx == 1


def test_brightest_in_adu_range_no_bounds_picks_first() -> None:
    policy = SELECTION_POLICIES["brightest_in_adu_range"]
    coords = np.array([[10.0, 10.0], [20.0, 20.0]])
    adu = np.array([100.0, 50.0])
    idx = policy(coords, adu, _state())  # no bounds in params
    assert idx == 0


# ---------------------------------------------------------------------------
# weighted_score
# ---------------------------------------------------------------------------


def test_weighted_score_balances_brightness_and_distance() -> None:
    policy = SELECTION_POLICIES["weighted_score"]
    coords = np.array([[120.0, 100.0], [101.0, 100.0]])
    adu = np.array([10_000.0, 1_000.0])
    state = _state()  # central=(100,100), reg_px=25
    idx = policy(coords, adu, state)
    # 10000/(1+400)=24.94, 1000/(1+1)=500 -> closer one wins
    assert idx == 1
