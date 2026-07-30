"""Unit tests for FiberPhotocentroidMethod — sign convention + noise gate.

Both defects locked here were found on sky 2026-07-29 (see
``doc/guider/NIGHT_REPORT_2026-07-29_stefan.md``):

- §2.1 — the correction sign was inverted vs the ``Correction``
  contract (``star − target``), double-negating with the pulse-guide
  model and driving the star *away* from the fibre.
- §2.2 — the detection gate was computed on the non-negative-clipped
  flux sum, which pure background noise passes 4× over threshold; the
  method "acquired" empty sky at a few ADU.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame
from ocabox_tcs.services.guiding_svc.stages.solver.methods.fiber_photocentroid import (
    FiberPhotocentroidMethod,
)
from ocabox_tcs.services.guiding_svc.stages.solver.methods.single_star import (
    SingleStarMethod,
)


CENTRAL = (100.0, 100.0)


def _gauss(
    shape: tuple[int, int],
    centre_xy: tuple[float, float],
    amplitude: float,
    fwhm: float = 3.0,
) -> np.ndarray:
    h, w = shape
    sigma = fwhm / 2.355
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = centre_xy
    g = amplitude * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2)))
    return g.astype(np.float32)


def _frame(
    stars: list[tuple[tuple[float, float], float]],
    *,
    shape: tuple[int, int] = (200, 200),
    background: float = 50.0,
    noise: float = 2.0,
    seed: int = 0,
) -> AnalysisFrame:
    rng = np.random.default_rng(seed)
    img = rng.normal(loc=background, scale=noise, size=shape).astype(np.float32)
    for xy, amp in stars:
        img = img + _gauss(shape, xy, amp)
    return AnalysisFrame(
        array=img, exp_time_total=1.0, n_stacked=1, timestamp=dt_utcnow_array(),
    )


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "central_point": CENTRAL,
        "exp_time": 1.0,
        "acquired": False,
        "acquired_pos": None,
        "acquired_adu": None,
        "active_pulse": None,
        "method_params": {},
    }
    base.update(overrides)
    return base


def _fiber_method(**overrides: Any) -> FiberPhotocentroidMethod:
    params: dict[str, Any] = dict(
        fiber_radius_px=5.0,
        analysis_radius_px=15.0,
        adu_sigma_threshold=3.0,
        hole_zone_factor=2.0,
    )
    params.update(overrides)
    method = FiberPhotocentroidMethod(**params)
    method.controller = AsyncMock()
    return method


# ---------------------------------------------------------------------------
# Sign convention (regression for the 2026-07-29 runaway)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_is_error_vector_star_minus_target() -> None:
    """Star displaced (+10, +6) from the reticle → ``dx≈+10, dy≈+6``.

    The ``Correction`` contract is the *measured error* — the model
    negates internally. Emitting ``target − star`` here reproduces the
    positive-feedback runaway."""
    method = _fiber_method()
    # d_apparent ≈ 11.7 px ≥ fiber_r·factor = 10 → full 1:1 regime.
    frame = _frame([((CENTRAL[0] + 10.0, CENTRAL[1] + 6.0), 8_000.0)])

    correction = await method.solve(frame, _state())

    assert correction is not None
    assert correction.dx_px == pytest.approx(10.0, abs=1.5)
    assert correction.dy_px == pytest.approx(6.0, abs=1.5)


@pytest.mark.asyncio
async def test_sign_convention_matches_single_star() -> None:
    """Both methods must report the SAME sign for the same physical
    scene — the pulse-guide model is calibrated once, method-agnostic."""
    offset = (12.0, -8.0)
    star = (CENTRAL[0] + offset[0], CENTRAL[1] + offset[1])
    frame = _frame([(star, 8_000.0)])

    fiber = _fiber_method(analysis_radius_px=20.0)
    fiber_corr = await fiber.solve(frame, _state())

    single = SingleStarMethod(fwhm=3.0, threshold=5.0)
    single.controller = AsyncMock()
    single_corr = await single.solve(
        frame,
        _state(
            wide_search_radius_px=50,
            search_reg_px=10,
            adu_match_tolerance_per_sec=5_000.0,
            # ``guide_anchor`` is the drift reference for single_star;
            # anchoring at the reticle makes both methods answer the
            # same question: "where is the star relative to CENTRAL?"
            guide_anchor=CENTRAL,
            acquired=True,
            acquired_pos=star,
        ),
    )

    assert fiber_corr is not None and single_corr is not None
    assert fiber_corr.dx_px == pytest.approx(single_corr.dx_px, abs=1.5)
    assert fiber_corr.dy_px == pytest.approx(single_corr.dy_px, abs=1.5)
    # Belt-and-braces: signs explicitly.
    assert fiber_corr.dx_px > 0 and single_corr.dx_px > 0
    assert fiber_corr.dy_px < 0 and single_corr.dy_px < 0


# ---------------------------------------------------------------------------
# Detection gate (regression for the noise-lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_noise_does_not_acquire() -> None:
    """Empty sky must NOT pass the flux gate — with the clipped-sum
    gate it passed ~4× over threshold and the UI showed a lock circle
    wandering over background at a few ADU."""
    method = _fiber_method()
    frame = _frame([], noise=5.0, seed=42)

    correction = await method.solve(frame, _state())

    assert correction is None
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is False


@pytest.mark.asyncio
async def test_pure_noise_rejected_across_seeds() -> None:
    """The gate must hold statistically, not for one lucky seed."""
    for seed in range(10):
        method = _fiber_method()
        frame = _frame([], noise=5.0, seed=seed)
        assert await method.solve(frame, _state()) is None, f"seed={seed}"


@pytest.mark.asyncio
async def test_banded_background_does_not_acquire() -> None:
    """Line-correlated readout structure (horizontal banding, as seen
    on the jk15 guider sensor) must NOT pass the gate. Bands violate
    the iid assumption behind √n statistics — a scalar-background gate
    passed on pure structure at ~40 ADU (observed live 2026-07-30);
    the destriping step removes it."""
    rng = np.random.default_rng(7)
    img = rng.normal(loc=50.0, scale=2.0, size=(200, 200)).astype(np.float32)
    # Horizontal bands: every other 4-row block +40 ADU (worse than live).
    for y0 in range(0, 200, 8):
        img[y0:y0 + 4, :] += 40.0
    frame = AnalysisFrame(
        array=img, exp_time_total=1.0, n_stacked=1, timestamp=dt_utcnow_array(),
    )
    method = _fiber_method()

    assert await method.solve(frame, _state()) is None
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is False


@pytest.mark.asyncio
async def test_star_on_banded_background_still_acquires() -> None:
    """Destriping must not throw the star out with the bands."""
    rng = np.random.default_rng(7)
    img = rng.normal(loc=50.0, scale=2.0, size=(200, 200)).astype(np.float32)
    for y0 in range(0, 200, 8):
        img[y0:y0 + 4, :] += 40.0
    img += _gauss((200, 200), (CENTRAL[0] + 8.0, CENTRAL[1]), 4_000.0)
    frame = AnalysisFrame(
        array=img, exp_time_total=1.0, n_stacked=1, timestamp=dt_utcnow_array(),
    )
    method = _fiber_method()

    correction = await method.solve(frame, _state())

    assert correction is not None
    # Sign/geometry survives destriping: error points toward the star.
    assert correction.dx_px > 0
    assert abs(correction.dy_px) < 2.0


@pytest.mark.asyncio
async def test_real_star_still_acquires() -> None:
    """Gate fix must not throw out the baby: a genuine star in the
    analysis window is detected and reported as acquired."""
    method = _fiber_method()
    frame = _frame([((CENTRAL[0] + 8.0, CENTRAL[1]), 6_000.0)], noise=5.0)

    correction = await method.solve(frame, _state())

    assert correction is not None
    kwargs = method.controller.notify_acquired.await_args.kwargs
    assert kwargs["acquired"] is True


# ---------------------------------------------------------------------------
# Hole-bias compensation regimes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_zone_emits_zero_correction() -> None:
    """Photocentroid inside the fibre radius → zero-magnitude
    correction (don't fight noise/PSF asymmetry inside the hole)."""
    method = _fiber_method()
    frame = _frame([(CENTRAL, 8_000.0)])

    correction = await method.solve(frame, _state())

    assert correction is not None
    assert correction.dx_px == pytest.approx(0.0)
    assert correction.dy_px == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_ramp_zone_shrinks_correction_magnitude() -> None:
    """Between ``fiber_r`` and ``fiber_r·factor`` the correction is
    ramped down (hole-bias compensation) but keeps the error's sign."""
    method = _fiber_method()
    # d_apparent ≈ 7.2 px — inside the (5, 10) ramp.
    frame = _frame([((CENTRAL[0] + 6.0, CENTRAL[1] + 4.0), 8_000.0)])

    correction = await method.solve(frame, _state())

    assert correction is not None
    assert 0 < correction.dx_px < 6.0 + 1.0
    assert 0 < correction.dy_px < 4.0 + 1.0
