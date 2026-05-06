"""Unit tests for SingleStarMethod — wide search + narrow re-acquisition.

Frames are synthetic Gaussian PSFs on a uniform background; FFS is
called for real (no mocking the detector).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame
from ocabox_tcs.services.guiding_svc.stages.solver.methods.single_star import (
    SingleStarMethod,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gauss(
    shape: tuple[int, int],
    centre_xy: tuple[float, float],
    amplitude: float,
    fwhm: float = 3.0,
) -> np.ndarray:
    """Generate a single 2D Gaussian PSF on a zero background.

    `centre_xy` is `(x, y)` = (column, row).
    """
    h, w = shape
    sigma = fwhm / 2.355
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = centre_xy
    g = amplitude * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2)))
    return g.astype(np.float32)


def _frame_with_stars(
    shape: tuple[int, int],
    stars: list[tuple[tuple[float, float], float]],
    background: float = 50.0,
    noise: float = 2.0,
    fwhm: float = 3.0,
    seed: int = 0,
) -> AnalysisFrame:
    """Compose a synthetic AnalysisFrame from a list of stars.

    Each star is `((x, y), amplitude_above_background)`.
    """
    rng = np.random.default_rng(seed)
    img = rng.normal(loc=background, scale=noise, size=shape).astype(np.float32)
    for (xy, amp) in stars:
        img = img + _gauss(shape, xy, amp, fwhm=fwhm)
    return AnalysisFrame(
        array=img,
        exp_time_total=1.0,
        n_stacked=1,
        timestamp=dt_utcnow_array(),
    )


def _state(**overrides: Any) -> dict[str, Any]:
    """Build a minimal state snapshot dict."""
    base: dict[str, Any] = {
        "central_point": (100.0, 100.0),
        "wide_search_radius_px": 50,
        "search_reg_px": 10,
        "exp_time": 1.0,
        "adu_match_tolerance_per_sec": 5_000.0,
        "acquired": False,
        "acquired_pos": None,
        "acquired_adu": None,
        "method_params": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Wide search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wide_search_picks_brightest_in_window() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # Bright star inside window, dim star also inside, far star outside.
    stars = [
        ((110.0, 95.0), 8_000.0),  # brightest, inside window (radius 50 from (100,100))
        ((90.0, 105.0), 2_000.0),  # dimmer, inside window
        ((10.0, 10.0), 12_000.0),  # brightest overall but OUTSIDE wide radius
    ]
    frame = _frame_with_stars((200, 200), stars)
    state = _state()

    correction = await method.solve(frame, state)
    assert correction is not None
    assert correction.method == "single_star"
    assert correction.metadata["phase"] == "wide_search"
    # Brightest within radius is the (110, 95) star.
    px, py = correction.metadata["star_pos"]
    assert abs(px - 110.0) < 2.0
    assert abs(py - 95.0) < 2.0
    # dx, dy = found - central
    assert abs(correction.dx_px - (px - 100.0)) < 1e-6
    assert abs(correction.dy_px - (py - 100.0)) < 1e-6
    # Controller should have been notified that we acquired.
    method.controller.notify_acquired.assert_awaited_once()
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is True
    assert kwargs["position"] is not None


@pytest.mark.asyncio
async def test_wide_search_returns_none_when_no_star_in_window() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # All stars outside the wide-search radius.
    stars = [
        ((10.0, 10.0), 8_000.0),
        ((190.0, 190.0), 9_000.0),
    ]
    frame = _frame_with_stars((200, 200), stars)
    state = _state()  # central=(100,100), radius=50

    correction = await method.solve(frame, state)
    assert correction is None
    # Should NOT mark as acquired.
    method.controller.notify_acquired.assert_not_called()


@pytest.mark.asyncio
async def test_wide_search_returns_none_on_empty_frame() -> None:
    method = SingleStarMethod()
    method.controller = AsyncMock()
    frame = _frame_with_stars((200, 200), stars=[], background=50.0, noise=1.0)
    correction = await method.solve(frame, _state())
    assert correction is None


# ---------------------------------------------------------------------------
# Narrow re-acquisition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrow_reacq_matches_within_adu_tolerance() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # Star drifted from (110, 95) to (112, 96), same ADU.
    stars = [((112.0, 96.0), 8_000.0)]
    frame = _frame_with_stars((200, 200), stars)
    state = _state(
        acquired=True,
        acquired_pos=(110.0, 95.0),
        acquired_adu=8_000.0,
        adu_match_tolerance_per_sec=5_000.0,
        exp_time=1.0,
        search_reg_px=15,
    )

    correction = await method.solve(frame, state)
    assert correction is not None
    assert correction.metadata["phase"] == "narrow"
    px, py = correction.metadata["star_pos"]
    assert abs(px - 112.0) < 2.0
    assert abs(py - 96.0) < 2.0
    method.controller.notify_acquired.assert_awaited_once()
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is True


@pytest.mark.asyncio
async def test_narrow_reacq_picks_closest_when_multiple_match() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # Two candidates inside crop, both within ADU tolerance; closer one wins.
    stars = [
        ((112.0, 96.0), 8_000.0),  # closer to (110,95), distance ~2.2
        ((116.0, 100.0), 8_500.0),  # farther but still in tolerance, distance ~7.8
    ]
    frame = _frame_with_stars((200, 200), stars)
    state = _state(
        acquired=True,
        acquired_pos=(110.0, 95.0),
        acquired_adu=8_000.0,
        adu_match_tolerance_per_sec=5_000.0,
        exp_time=1.0,
        search_reg_px=20,
    )

    correction = await method.solve(frame, state)
    assert correction is not None
    px, py = correction.metadata["star_pos"]
    assert abs(px - 112.0) < 2.0
    assert abs(py - 96.0) < 2.0


@pytest.mark.asyncio
async def test_narrow_reacq_demotes_when_no_star() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # Empty crop region: no star anywhere near acquired_pos.
    stars: list = []  # no stars at all
    frame = _frame_with_stars((200, 200), stars)
    state = _state(
        acquired=True,
        acquired_pos=(110.0, 95.0),
        acquired_adu=8_000.0,
        search_reg_px=10,
    )

    correction = await method.solve(frame, state)
    assert correction is None
    # Demotion notification.
    method.controller.notify_acquired.assert_awaited_once()
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is False
    assert kwargs["position"] is None


@pytest.mark.asyncio
async def test_narrow_reacq_demotes_when_adu_outside_tolerance() -> None:
    method = SingleStarMethod(fwhm=3.0, threshold=5.0)
    method.controller = AsyncMock()

    # Star present but ADU way off the locked value (outside tolerance).
    stars = [((112.0, 96.0), 1_000.0)]
    frame = _frame_with_stars((200, 200), stars, noise=2.0)
    state = _state(
        acquired=True,
        acquired_pos=(110.0, 95.0),
        acquired_adu=8_000.0,
        adu_match_tolerance_per_sec=500.0,
        exp_time=1.0,
        search_reg_px=10,
    )

    correction = await method.solve(frame, state)
    assert correction is None
    method.controller.notify_acquired.assert_awaited_once()
    assert (
        method.controller.notify_acquired.await_args.kwargs["acquired"] is False
    )


@pytest.mark.asyncio
async def test_narrow_reacq_demotes_when_inconsistent_state() -> None:
    """acquired=True but acquired_pos=None — state inconsistency."""
    method = SingleStarMethod()
    method.controller = AsyncMock()
    frame = _frame_with_stars((200, 200), stars=[((100.0, 100.0), 8_000.0)])
    state = _state(acquired=True, acquired_pos=None, acquired_adu=8_000.0)
    correction = await method.solve(frame, state)
    assert correction is None
    method.controller.notify_acquired.assert_awaited_once()


# ---------------------------------------------------------------------------
# Confidence + controller wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_is_adu_fraction_of_saturation() -> None:
    method = SingleStarMethod(saturation_adu=10_000.0)
    method.controller = AsyncMock()
    stars = [((110.0, 95.0), 5_000.0)]
    frame = _frame_with_stars((200, 200), stars)
    correction = await method.solve(frame, _state())
    assert correction is not None
    # Detection picks up roughly the peak ADU value (plus background).
    assert 0.0 < correction.confidence <= 1.0


@pytest.mark.asyncio
async def test_no_controller_means_no_crash() -> None:
    """sim/dev path: controller may not be wired."""
    method = SingleStarMethod()
    # No controller assignment.
    stars = [((110.0, 95.0), 8_000.0)]
    frame = _frame_with_stars((200, 200), stars)
    correction = await method.solve(frame, _state())
    assert correction is not None
    assert correction.metadata["phase"] == "wide_search"
