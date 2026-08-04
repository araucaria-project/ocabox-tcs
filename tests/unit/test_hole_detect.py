"""Reticle-target (fibre-entrance) detector — behaviour and safety gates.

The detector's job is not merely to find the hole; it is to *refuse*
whenever a wrong answer is plausible, because a bad refinement moves the
aim point off the feature and costs throughput. These tests therefore
weight refusal cases at least as heavily as detections.

Synthetic frames model the real physics: a Gaussian PSF partly swallowed
by a circular absorber, on sky background with photon + read noise and
the sensor's line-correlated readout banding.
"""

from __future__ import annotations

import numpy as np
import pytest

from ocabox_tcs.services.guiding_svc.hole_detect import (
    HoleDetectConfig,
    HoleTracker,
    detect_hole,
)


R = 5.0
SIZE = 200
CENTRE = (100.0, 100.0)


def make_frame(
    hole_pos: tuple[float, float],
    star_offset: tuple[float, float] = (0.0, 0.0),
    *,
    fwhm: float = 10.0,
    peak: float = 30_000.0,
    sky: float = 300.0,
    read_noise: float = 8.0,
    banding: float = 15.0,
    saturation: float = 62_000.0,
    seed: int = 7,
) -> np.ndarray:
    """Star at ``hole_pos + star_offset``, absorbed inside the hole."""
    rng = np.random.default_rng(seed)
    sigma = fwhm / 2.355
    yy, xx = np.indices((SIZE, SIZE)).astype(float)
    sx, sy = hole_pos[0] + star_offset[0], hole_pos[1] + star_offset[1]
    img = peak * np.exp(-(((xx - sx) ** 2 + (yy - sy) ** 2) / (2 * sigma**2)))
    img[((xx - hole_pos[0]) ** 2 + (yy - hole_pos[1]) ** 2) <= R * R] = 0.0
    img += sky + banding * rng.standard_normal((SIZE, 1))
    img = rng.poisson(np.clip(img, 0, None)).astype(float)
    img += read_noise * rng.standard_normal((SIZE, SIZE))
    return np.clip(img, 0, saturation)


def blank_frame(seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.poisson(300.0, (SIZE, SIZE)).astype(float)
    img += 8.0 * rng.standard_normal((SIZE, SIZE))
    img += 15.0 * rng.standard_normal((SIZE, 1))
    return img


@pytest.fixture
def cfg() -> HoleDetectConfig:
    return HoleDetectConfig(radius_px=R, search_radius_px=25.0)


class FakeClock:
    """Deterministic monotonic clock; tests advance it explicitly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float = 1.0) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# Detection accuracy — the primary use case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("true_offset", [0.0, 1.0, 3.0, 5.0, 8.0, 12.0])
def test_finds_displaced_hole_when_source_is_on_it(cfg, true_offset):
    """The case that matters: the star is on the entrance, but the stored
    reticle is stale by ``true_offset`` px. Recovery must be well inside a
    pixel, since the thermal drift being corrected is itself only a few
    pixels and a comparable error would defeat the purpose."""
    hole = (CENTRE[0] + true_offset, CENTRE[1] - 0.4 * true_offset)
    det, reason = detect_hole(make_frame(hole), CENTRE, cfg)
    assert det is not None, f"missed the hole: {reason}"
    assert np.hypot(det.x - hole[0], det.y - hole[1]) < 0.6
    assert det.offset_px == pytest.approx(
        np.hypot(hole[0] - CENTRE[0], hole[1] - CENTRE[1]), abs=0.6
    )


def test_detection_survives_saturated_core(cfg):
    """A saturated PSF core is *helpful* here (maximum rim contrast) and
    must not break the estimate, unlike in centroid methods where the
    flat plateau quantises the answer."""
    hole = (CENTRE[0] + 3.0, CENTRE[1])
    det, reason = detect_hole(make_frame(hole, peak=62_000.0), CENTRE, cfg)
    assert det is not None, reason
    assert np.hypot(det.x - hole[0], det.y - hole[1]) < 0.6


def test_works_under_flat_illumination(cfg):
    """Flat-lamp case: uniform bright field, hole is a plain dark disk.
    No star, no halo gradient — the ring is lit evenly, which is the
    easiest possible geometry for the all-round-lit gate."""
    rng = np.random.default_rng(3)
    yy, xx = np.indices((SIZE, SIZE)).astype(float)
    hole = (CENTRE[0] + 2.5, CENTRE[1] - 1.5)
    img = np.full((SIZE, SIZE), 12_000.0)
    img[((xx - hole[0]) ** 2 + (yy - hole[1]) ** 2) <= R * R] = 200.0
    img = rng.poisson(img).astype(float)
    det, reason = detect_hole(img, CENTRE, cfg)
    assert det is not None, reason
    assert np.hypot(det.x - hole[0], det.y - hole[1]) < 0.6


# ---------------------------------------------------------------------------
# Refusals — each one is a wrong answer we are choosing not to give
# ---------------------------------------------------------------------------


def test_refuses_blank_sky(cfg):
    """No source anywhere: a matched filter on pure noise always has a
    maximum somewhere, so the halo gate is what stops us from reporting
    it as an entrance."""
    det, reason = detect_hole(blank_frame(), CENTRE, cfg)
    assert det is None
    assert "no light" in reason


def test_refuses_when_source_is_far_from_the_hole(cfg):
    """Dark sky *beside* a bright star scores as well on the matched
    filter as a real hole does. Without the all-round-lit gate the
    detector reports a position ~14 px wrong; it must refuse instead."""
    hole = (CENTRE[0] + 3.0, CENTRE[1])
    det, reason = detect_hole(make_frame(hole, star_offset=(25.0, 0.0)), CENTRE, cfg)
    assert det is None
    assert "one-sided" in reason


def test_refuses_when_star_hides_entirely_in_the_hole(cfg):
    """Excellent seeing, PSF much smaller than the hole: essentially no
    light escapes, so there is nothing to locate. Reporting "no light"
    (rather than a guess) is the correct, honest answer."""
    hole = (CENTRE[0] + 3.0, CENTRE[1])
    det, reason = detect_hole(make_frame(hole, fwhm=3.0), CENTRE, cfg)
    assert det is None
    assert "no light" in reason


def test_refuses_near_frame_edge(cfg):
    """A clipped search window biases the offset toward the frame
    interior, so refuse rather than report a skewed measurement."""
    det, reason = detect_hole(make_frame((10.0, 10.0)), (10.0, 10.0), cfg)
    assert det is None
    assert "edge" in reason


def test_hole_outside_search_radius_is_not_reported_far_away(cfg):
    """Bounding the search also bounds the damage: whatever happens, the
    reported candidate cannot be further than ``search_radius_px``."""
    hole = (CENTRE[0] + 60.0, CENTRE[1])
    det, _ = detect_hole(make_frame(hole), CENTRE, cfg)
    if det is not None:
        assert det.offset_px <= cfg.search_radius_px + 1.0


def test_faint_source_tightens_the_gate(cfg):
    """The all-round-lit test references noise, so it scales with signal:
    a marginal source a few px off centre is refused, while the same
    geometry with a bright source is accepted."""
    hole = (CENTRE[0] + 3.0, CENTRE[1])
    faint, reason = detect_hole(
        make_frame(hole, star_offset=(4.0, 0.0), peak=5_000.0), CENTRE, cfg
    )
    bright, _ = detect_hole(
        make_frame(hole, star_offset=(4.0, 0.0), peak=30_000.0), CENTRE, cfg
    )
    assert faint is None and "one-sided" in reason
    assert bright is not None


def test_disabled_config_yields_nothing(cfg):
    tracker = HoleTracker(HoleDetectConfig(enabled=False), clock=FakeClock())
    assert tracker.update(make_frame(CENTRE), CENTRE) is None


# ---------------------------------------------------------------------------
# Tracker — multi-frame evidence and the refinable gate
# ---------------------------------------------------------------------------


def test_tracker_requires_consistent_frames_before_offering_refinement(cfg):
    """``refinable`` is what the UI enables its button from, so the
    ramp-up must be explicit: not offered until ``min_samples`` mutually
    consistent measurements exist."""
    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    hole = (CENTRE[0] + 4.0, CENTRE[1] - 2.0)
    seen_refinable = []
    for i in range(cfg.min_samples + 2):
        clock.tick()
        cand = tracker.update(make_frame(hole, seed=i), CENTRE, cfg)
        assert cand is not None
        seen_refinable.append(cand.refinable)
    assert seen_refinable[: cfg.min_samples - 1] == [False] * (cfg.min_samples - 1)
    assert seen_refinable[-1] is True
    final = tracker.update(make_frame(hole, seed=99), CENTRE, cfg)
    assert np.hypot(final.x - hole[0], final.y - hole[1]) < 0.6
    assert "ready" in final.reason


def test_tracker_never_offers_refinement_on_blank_sky(cfg):
    """The safety property that matters most: no amount of noise may
    ever talk the tracker into offering a refinement."""
    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    for i in range(20):
        clock.tick()
        cand = tracker.update(blank_frame(seed=i), CENTRE, cfg)
        assert cand is None or not cand.refinable


def test_tracker_ages_out_stale_evidence(cfg):
    """Evidence gathered before the operator moved on must not vote: a
    gap longer than ``max_age_s`` empties the window."""
    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    hole = (CENTRE[0] + 4.0, CENTRE[1])
    for i in range(cfg.min_samples + 1):
        clock.tick()
        tracker.update(make_frame(hole, seed=i), CENTRE, cfg)
    assert tracker.update(make_frame(hole), CENTRE, cfg).refinable

    clock.tick(cfg.max_age_s + 1.0)
    cand = tracker.update(blank_frame(), CENTRE, cfg)
    assert cand is None


def test_tracker_reset_discards_evidence(cfg):
    """Called when the aim point moves — samples were judged against the
    previous centre and must not carry over."""
    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    hole = (CENTRE[0] + 4.0, CENTRE[1])
    for i in range(cfg.min_samples + 1):
        clock.tick()
        tracker.update(make_frame(hole, seed=i), CENTRE, cfg)
    tracker.reset()
    clock.tick()
    cand = tracker.update(make_frame(hole), CENTRE, cfg)
    assert cand is not None
    assert not cand.refinable
    assert cand.samples == 1


def test_already_centred_is_not_refinable(cfg):
    """Zero-offset means there is nothing to fix; offering the action
    would invite pointless churn of a calibrated value."""
    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    for i in range(cfg.min_samples + 2):
        clock.tick()
        cand = tracker.update(make_frame(CENTRE, seed=i), CENTRE, cfg)
    assert not cand.refinable
    assert "nothing to refine" in cand.reason


def test_large_offset_is_measured_but_refused(cfg):
    """A big apparent jump is far more likely a misdetection than a real
    mechanical shift, so it is reported (operator can see it) but not
    offered as a one-click action."""
    tight = HoleDetectConfig(
        radius_px=R, search_radius_px=25.0, refine_max_offset_px=5.0
    )
    clock = FakeClock()
    tracker = HoleTracker(tight, clock=clock)
    hole = (CENTRE[0] + 12.0, CENTRE[1])
    for i in range(tight.min_samples + 2):
        clock.tick()
        cand = tracker.update(make_frame(hole, seed=i), CENTRE, tight)
    assert cand is not None
    assert not cand.refinable
    assert "safety limit" in cand.reason
    assert cand.offset_px > 5.0


def test_candidate_payload_is_json_friendly(cfg):
    """The candidate travels to the UI inside the state snapshot, so it
    must contain only plain JSON types."""
    import json

    clock = FakeClock()
    tracker = HoleTracker(cfg, clock=clock)
    clock.tick()
    payload = tracker.update(make_frame((CENTRE[0] + 4, CENTRE[1])), CENTRE, cfg).to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {
        "x", "y", "offset_px", "snr", "scatter_px", "samples",
        "refinable", "reason", "ts_monotonic",
    }
