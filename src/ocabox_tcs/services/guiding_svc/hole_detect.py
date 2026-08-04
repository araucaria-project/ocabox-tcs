"""Locate the guider's aim point in the image instead of trusting config.

``central_point`` (the reticle) is a configured pixel that is supposed to
coincide with a physical feature — a spectrograph fibre entrance, a
pinhole, a slit. Such a feature moves with temperature, so a value
calibrated once drifts out of date, and every solver method will
faithfully centre light on the stale pixel.

When light falls on the feature it becomes visible: it absorbs, and shows
up as a dark disk inside a bright halo. This module measures that disk,
so the aim point can be re-referenced to a measurement.

The concept is generic — the reticle is a universal guider notion and a
fibre entrance is one application of it. Fibre wording belongs in the
fibre-specific solver method and in instrument manuals, not here.

Algorithm: matched filter for a dark disk on a bright surround,

    score(p) = mean(annulus around p) − mean(disk at p)

maximised over a bounded search grid, with 3-point parabolic sub-pixel
interpolation at the peak. Being a difference of local means it is immune
to an additive pedestal (sky level, linear gradient); only the "bright
ring around dark middle" signature survives. Residual sensitivity to the
*curvature* of a halo across the template is a known bias — see
``doc/guider/reticle-target-detection.md``.

Two design rules run through the module:

- **It runs independently of solver method and mode**, so an operator can
  measure and re-reference before committing to a method that depends on
  the aim point being right.
- **It refuses rather than guesses.** A wrong re-reference moves the aim
  point off the feature, which is worse than a stale value someone knows
  about. Every gate yields a human-readable ``reason``, and
  :class:`HoleTracker` additionally requires agreement across frames —
  the feature is static, so cross-frame consistency is the available
  evidence that a real feature is being measured rather than noise, a
  cosmic-ray shadow or a dust mote.
"""

from __future__ import annotations

import logging
import time
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


@dataclass
class HoleDetectConfig:
    """Tuning surface for reticle-target detection.

    Defaults are for the jk15 BESO guider (hole radius ≈ 5 px on a
    1936×1216 ASI174MM), but nothing here is instrument-specific in
    kind.
    """

    enabled: bool = True
    #: Geometric radius of the absorbing feature (px). For fibre feeds
    #: this equals the solver's ``fiber_radius_px``; kept separate so
    #: detection works while a non-fibre method is active.
    #:
    #: **Precision constant, not a threshold**: as a matched-filter
    #: template radius, 1 px of error here costs 1.2–1.9 px of reported
    #: position, where the same number as a dead-zone width tolerates
    #: ~20 % error harmlessly. Measure it on an illuminated or flat frame
    #: rather than inheriting the dead-zone value.
    radius_px: float = 5.0
    #: How far from the assumed centre to search (px). Bounds the cost
    #: and the worst-case damage: a candidate can never be reported
    #: further away than this.
    #:
    #: Keep it tight. The rate of spurious maxima grows with the search
    #: radius and they appear *on the boundary*; a wide radius rests the
    #: whole defence on the per-sector contrast test below. Sizing it to
    #: the drift actually expected (single-digit px) costs no capability.
    search_radius_px: float = 12.0
    #: Gap between hole disk and reference annulus (px) — skips the
    #: blurred rim, which belongs to neither.
    annulus_gap_px: float = 1.5
    #: Radial width of the reference annulus (px).
    annulus_width_px: float = 5.0
    #: Matched-filter SNR required of a single-frame detection.
    min_snr: float = 6.0
    #: Every azimuthal wedge of the reference annulus must exceed the
    #: hole's own level by this many sigma — "the ring is brighter than
    #: the middle, on every side".
    #:
    #: Contrast against the hole, not absolute brightness: the window is
    #: destriped (row/column median subtraction) first, which removes any
    #: uniform pedestal. An absolute-level test would therefore fail on a
    #: uniformly illuminated field — which is *entirely* pedestal — and
    #: illuminating the feature is the most deliberate way to measure it.
    #:
    #: One statistic, two distinguishable failures: no sector reaching
    #: contrast means nothing is lighting the feature; only some sectors
    #: reaching it means the light is one-sided, i.e. a source sitting
    #: elsewhere whose neighbouring sky scores just as well on the
    #: matched filter as a genuine hole.
    min_sector_contrast_sigma: float = 3.0
    #: Reject when a rival peak (outside the winner's exclusion disk)
    #: reaches this fraction of the best score — the field contains
    #: something else that looks equally hole-like.
    max_rival_ratio: float = 0.7
    #: Number of azimuthal wedges for the all-round-lit test.
    annulus_sectors: int = 8
    #: Tracker: rolling window of detections considered (frames).
    window_frames: int = 12
    #: Tracker: detections older than this are dropped (s). Keeps a
    #: refinement from acting on evidence gathered before the operator
    #: slewed somewhere else entirely.
    max_age_s: float = 20.0
    #: Tracker: how many consistent detections are needed.
    min_samples: int = 5
    #: Tracker: allowed scatter (MAD, px) across the window. Tight,
    #: because a static feature measured on a static field should not
    #: wander; scatter means we are tracking noise.
    max_scatter_px: float = 1.5
    #: Refinement gate: below this the move is pointless (and within
    #: measurement noise) — nothing to refine.
    refine_min_offset_px: float = 0.3
    #: Refinement gate: above this we refuse. A large apparent jump is
    #: far more likely a misdetection than a real mechanical shift; the
    #: operator can still right-click deliberately.
    refine_max_offset_px: float = 10.0


@dataclass
class HoleDetection:
    """Single-frame detection result."""

    x: float
    y: float
    offset_px: float
    snr: float
    contrast: float
    halo_level: float
    noise: float


@dataclass
class HoleCandidate:
    """Tracked, multi-frame candidate — the thing the UI gates on.

    ``refinable`` is the authoritative "the button may be enabled"
    flag; ``reason`` explains the state either way, in operator
    language.
    """

    x: float
    y: float
    offset_px: float
    snr: float
    scatter_px: float
    samples: int
    refinable: bool
    reason: str
    ts_monotonic: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "offset_px": round(self.offset_px, 3),
            "snr": round(self.snr, 2),
            "scatter_px": round(self.scatter_px, 3),
            "samples": self.samples,
            "refinable": self.refinable,
            "reason": self.reason,
            # Monotonic clock, not wall time: the only consumer is the
            # service's own staleness check on refine, and monotonic is
            # immune to NTP steps. UI shows ``reason``, not an age.
            "ts_monotonic": round(self.ts_monotonic, 3),
        }


def detect_hole(
    array: np.ndarray,
    assumed_center: tuple[float, float],
    cfg: HoleDetectConfig,
) -> tuple[HoleDetection | None, str]:
    """Find the absorbing feature near ``assumed_center``.

    Returns ``(detection, reason)``. ``detection`` is None when any gate
    fails; ``reason`` always carries a short explanation (also on
    success, where it is ``"ok"``).
    """
    if array.size == 0:
        return None, "no frame"

    R = float(cfg.radius_px)
    gap = float(cfg.annulus_gap_px)
    width = float(cfg.annulus_width_px)
    search = float(cfg.search_radius_px)

    # Kernel half-size: the annulus outer edge. Patch side is odd so the
    # kernel has a well-defined centre pixel.
    k_half = int(np.ceil(R + gap + width))
    k = 2 * k_half + 1
    s_half = int(np.ceil(search))

    H, W = array.shape
    cx, cy = float(assumed_center[0]), float(assumed_center[1])
    cx_int, cy_int = int(round(cx)), int(round(cy))
    # Window must hold every kernel placement across the search grid.
    half = s_half + k_half
    x0, x1 = cx_int - half, cx_int + half + 1
    y0, y1 = cy_int - half, cy_int + half + 1
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
        # Refuse rather than silently searching a clipped, off-centre
        # window: the reported offset would be biased toward the frame
        # interior.
        return None, "reticle too close to frame edge"

    window = array[y0:y1, x0:x1].astype(np.float64)

    # Background/gradient removal by row-then-column median subtraction.
    # The score is already background-differential, so this mainly buys
    # an honest noise estimate (the sensor shows line-correlated readout
    # banding, see fiber_photocentroid for the full story).
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        work = window - np.median(window, axis=1, keepdims=True)
        work = work - np.median(work, axis=0, keepdims=True)

    mad = float(np.median(np.abs(work - np.median(work))))
    noise = 1.4826 * mad
    if noise <= 0:
        noise = float(np.std(work)) or 1.0

    # Kernels: dark disk and concentric reference annulus.
    yy, xx = np.indices((k, k), dtype=np.float64)
    rr = np.hypot(xx - k_half, yy - k_half)
    disk = rr <= R
    annulus = (rr >= R + gap) & (rr <= R + gap + width)
    n_disk, n_ann = int(disk.sum()), int(annulus.sum())
    if n_disk == 0 or n_ann == 0:
        return None, "degenerate detector geometry (check radius/annulus config)"
    disk_w = disk / n_disk
    ann_w = annulus / n_ann

    # Matched filter over the whole search grid at once.
    patches = np.lib.stride_tricks.sliding_window_view(work, (k, k))
    disk_mean = np.tensordot(patches, disk_w, axes=((2, 3), (0, 1)))
    ann_mean = np.tensordot(patches, ann_w, axes=((2, 3), (0, 1)))
    score = ann_mean - disk_mean

    # Grid index (0,0) places the kernel centre at window pixel
    # (k_half, k_half) = sensor pixel (x0 + k_half, y0 + k_half), which
    # is (cx_int - s_half, cy_int - s_half).
    gi = int(np.argmax(score))
    gy, gx = np.unravel_index(gi, score.shape)
    best = float(score[gy, gx])
    if best <= 0:
        return None, "no dark feature near reticle"

    # Physical plausibility first, so that a blank or one-sided field is
    # described as such. The statistical gates below would also reject
    # these cases, but "weak signature (SNR 3.2 < 6)" tells the operator
    # nothing about what to do, whereas "no light at the reticle" does.
    # Every side of the ring must stand above the hole's own level.
    patch = work[gy:gy + k, gx:gx + k]
    floor = cfg.min_sector_contrast_sigma * noise
    sector_contrast = _annulus_sector_contrast(
        patch, annulus, k_half, cfg.annulus_sectors, float(disk_mean[gy, gx])
    )
    lit = [c >= floor for c in sector_contrast]
    if not any(lit):
        # Nothing is lighting the entrance: normal during plain guiding,
        # and also the honest answer in excellent seeing when the star
        # disappears into the hole and no light escapes to reveal it.
        return None, "no light at the reticle — illuminate it or park a star on it"
    if not all(lit):
        return None, (
            "light is one-sided — centre a source on the reticle "
            f"({sum(lit)}/{len(lit)} sides lit)"
        )

    # Noise of the difference of two independent local means.
    sigma_score = noise * float(np.sqrt(1.0 / n_disk + 1.0 / n_ann))
    snr = best / sigma_score if sigma_score > 0 else 0.0
    if snr < cfg.min_snr:
        return None, f"weak signature (SNR {snr:.1f} < {cfg.min_snr:.0f})"

    # Uniqueness: is there a comparable rival elsewhere in the grid? Only
    # *non-overlapping* placements count as rivals — the score map has a
    # plateau of order the kernel size around the true peak (neighbouring
    # placements share most of their pixels), so excluding merely the hole
    # radius would make every genuine detection look ambiguous.
    gyy, gxx = np.indices(score.shape)
    rival_mask = np.hypot(gxx - gx, gyy - gy) > (2.0 * (R + gap + width))
    rival = float(score[rival_mask].max()) if rival_mask.any() else 0.0
    if rival > cfg.max_rival_ratio * best:
        return None, "ambiguous — more than one hole-like feature in range"

    halo = float(ann_mean[gy, gx])

    # Sub-pixel peak by parabolic interpolation, guarded against grid
    # edges (a peak on the border means the true one may lie outside
    # the search radius).
    if not (0 < gx < score.shape[1] - 1 and 0 < gy < score.shape[0] - 1):
        return None, "candidate at search-radius edge — move the reticle closer first"
    dx = _parabolic_shift(score[gy, gx - 1], best, score[gy, gx + 1])
    dy = _parabolic_shift(score[gy - 1, gx], best, score[gy + 1, gx])

    x = float(cx_int - s_half + gx + dx)
    y = float(cy_int - s_half + gy + dy)
    offset = float(np.hypot(x - cx, y - cy))
    return (
        HoleDetection(
            x=x, y=y, offset_px=offset, snr=float(snr),
            contrast=best, halo_level=halo, noise=noise,
        ),
        "ok",
    )


def _annulus_sector_contrast(
    patch: np.ndarray,
    annulus: np.ndarray,
    k_half: int,
    sectors: int,
    disk_level: float,
) -> list[float]:
    """Per-wedge excess of the reference annulus over the hole's level.

    One value per azimuthal wedge. The caller compares each against a
    noise floor: all wedges above ⇒ a dark disk surrounded by light on
    every side; none above ⇒ nothing is illuminating the feature; some
    above ⇒ the light is one-sided, so the candidate is more likely sky
    beside a star than a fed entrance.
    """
    if patch.shape != annulus.shape:
        return []
    sectors = max(4, int(sectors))
    yy, xx = np.indices(annulus.shape)
    theta = np.arctan2(yy - k_half, xx - k_half)
    bins = ((theta + np.pi) / (2 * np.pi) * sectors).astype(int) % sectors
    out: list[float] = []
    for s in range(sectors):
        m = annulus & (bins == s)
        if not m.any():
            return []
        out.append(float(patch[m].mean()) - disk_level)
    return out


def _parabolic_shift(left: float, centre: float, right: float) -> float:
    """Sub-pixel offset of a parabola through three samples, clamped to
    ±0.5 px (a larger value means the peak is not where argmax said)."""
    denom = left - 2.0 * centre + right
    if denom == 0:
        return 0.0
    shift = 0.5 * (left - right) / denom
    return float(np.clip(shift, -0.5, 0.5))


class HoleTracker:
    """Accumulates detections across frames into a trustworthy candidate.

    Why a tracker and not a per-frame answer: the entrance is static on
    guiding timescales, so consistency across frames is the strongest
    available evidence that we are looking at the real thing rather than
    at noise, a hot-pixel cluster, or a passing cosmic ray. The tracker
    also improves accuracy — the reported position is a per-axis median
    over the window.
    """

    def __init__(self, cfg: HoleDetectConfig | None = None,
                 clock: Any = time.monotonic) -> None:
        self.cfg = cfg or HoleDetectConfig()
        self._clock = clock
        self._samples: deque[tuple[float, float, float, float]] = deque(
            maxlen=max(2, self.cfg.window_frames)
        )
        self._last_reason = "no measurement yet"

    def reset(self) -> None:
        """Drop accumulated evidence. Called when the aim point moves
        (refine, right-click, home) — samples measured against the old
        assumed centre must not vote on the new one."""
        self._samples.clear()
        self._last_reason = "no measurement yet"

    def update(
        self,
        array: np.ndarray,
        assumed_center: tuple[float, float],
        cfg: HoleDetectConfig | None = None,
    ) -> HoleCandidate | None:
        """Process one frame; return the current candidate (or None when
        there is not enough evidence to report anything)."""
        if cfg is not None:
            if cfg.window_frames != self.cfg.window_frames:
                self._samples = deque(self._samples, maxlen=max(2, cfg.window_frames))
            self.cfg = cfg
        cfg = self.cfg
        if not cfg.enabled:
            return None

        now = float(self._clock())
        detection, reason = detect_hole(array, assumed_center, cfg)
        self._last_reason = reason
        if detection is not None:
            self._samples.append((now, detection.x, detection.y, detection.snr))

        # Age out stale evidence.
        while self._samples and (now - self._samples[0][0]) > cfg.max_age_s:
            self._samples.popleft()

        if not self._samples:
            return None

        arr = np.asarray([(s[1], s[2], s[3]) for s in self._samples], dtype=np.float64)
        x = float(np.median(arr[:, 0]))
        y = float(np.median(arr[:, 1]))
        snr = float(np.median(arr[:, 2]))
        scatter = float(
            np.median(np.abs(arr[:, 0] - x)) + np.median(np.abs(arr[:, 1] - y))
        )
        n = len(self._samples)
        offset = float(np.hypot(x - assumed_center[0], y - assumed_center[1]))

        refinable, why = self._judge(n, scatter, snr, offset, reason)
        return HoleCandidate(
            x=x, y=y, offset_px=offset, snr=snr, scatter_px=scatter,
            samples=n, refinable=refinable, reason=why, ts_monotonic=now,
        )

    def _judge(
        self, n: int, scatter: float, snr: float, offset: float, last_reason: str
    ) -> tuple[bool, str]:
        """Decide whether a refinement may be offered, with a reason
        phrased for the operator."""
        cfg = self.cfg
        if n < cfg.min_samples:
            if last_reason not in ("ok", "no measurement yet"):
                # Surface *why* frames are being rejected — far more
                # useful than a bare sample count.
                return False, last_reason
            return False, f"measuring… ({n}/{cfg.min_samples} frames)"
        if scatter > cfg.max_scatter_px:
            return False, f"unstable measurement (scatter {scatter:.1f} px)"
        if snr < cfg.min_snr:
            return False, f"weak signature (SNR {snr:.1f})"
        if offset < cfg.refine_min_offset_px:
            return False, "already centred — nothing to refine"
        if offset > cfg.refine_max_offset_px:
            return False, (
                f"offset {offset:.1f} px exceeds the {cfg.refine_max_offset_px:.0f} px "
                "safety limit — verify by eye and right-click instead"
            )
        return True, f"ready: move reticle {offset:.1f} px onto the measured hole"
