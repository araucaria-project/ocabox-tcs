# Reticle-target detection

Audience: whoever maintains or configures the guider.

## What it does

`central_point` — the reticle — is the pixel the guider aims light at. In
a spectrograph feed it is supposed to coincide with the fibre entrance,
but that entrance moves with temperature, so a configured constant goes
out of date within a night. Every solver method will keep centring light
on a stale pixel without complaint, and the light lands beside the
entrance instead of in it.

This component measures the entrance in the image and lets the operator
re-reference the reticle to that measurement.

The concept is deliberately generic: the reticle is a universal guider
notion, and a fibre entrance is one application. Anything that absorbs
light at the aim point — pinhole, slit — works the same way. Fibre
wording belongs in the fibre-specific solver method and in instrument
manuals.

## How the measurement works

When light falls on the entrance it becomes visible: it swallows light
and appears as a dark disk inside a bright halo. The detector is a
matched filter for exactly that,

    score(p) = mean(annulus around p) − mean(disk at p)

maximised over a bounded search grid around the assumed centre, with
3-point parabolic sub-pixel interpolation at the peak. Being a difference
of local means, it is immune to an additive pedestal — sky level, linear
gradient — so only the "bright ring around a dark middle" signature
survives.

`HoleTracker` accumulates single-frame detections and reports a per-axis
median. The entrance is static on guiding timescales, so agreement across
frames is the available evidence that a real feature is being measured
rather than noise, a cosmic-ray shadow or a dust mote — and the median is
more accurate than any single frame.

Cost is a few milliseconds per frame (vectorised via
`sliding_window_view` + `tensordot`). It runs in the Solver after the
active method, independently of which method and mode are selected, so a
refinement can be prepared before switching to a method that depends on
the aim point being right. A detector exception costs a candidate, never
a correction.

## Refusing is the primary feature

A wrong refinement moves the aim point *off* the entrance, which is worse
than a stale value someone knows about. Every gate carries text meant for
the operator, exposed as `hole_candidate.reason`:

| Condition | Reported reason |
|---|---|
| No side of the ring exceeds the hole's level | `no light at the reticle — illuminate it or park a star on it` |
| Only some sides do | `light is one-sided — centre a source on the reticle (5/8 sides lit)` |
| Matched-filter SNR below `min_snr` | `weak signature (SNR 4.1 < 6)` |
| A comparable rival peak elsewhere | `ambiguous — more than one hole-like feature in range` |
| Peak on the search boundary | `candidate at search-radius edge — move the reticle closer first` |
| Reticle too near the frame border | `reticle too close to frame edge` |
| Fewer than `min_samples` consistent frames | `measuring… (3/5 frames)` |
| Cross-frame scatter above `max_scatter_px` | `unstable measurement (scatter 2.1 px)` |
| Offset below `refine_min_offset_px` | `already centred — nothing to refine` |
| Offset above `refine_max_offset_px` | `offset 24.0 px exceeds the 20 px safety limit — verify by eye and right-click instead` |

Two of these carry most of the weight and are worth understanding before
changing them.

**Per-sector contrast.** Dark sky *beside* a bright star scores as well on
the matched filter as a real hole does; without this test the detector
reports a position 14 px wrong when the star is 25 px from the entrance.
Requiring every azimuthal wedge of the annulus to exceed the hole's own
level rejects that, because a fed entrance is lit from all sides. The test
uses ring-minus-hole *contrast* rather than absolute brightness on
purpose: the window is destriped first, which removes any uniform
pedestal, so an absolute-brightness test would fail on a uniformly
illuminated field — and illuminating the entrance is the most deliberate
way to measure it.

**Boundary rejection plus a tight search radius.** The rate of spurious
maxima grows with the search radius and they turn up on the boundary.
Sizing the radius to the drift actually expected (single-digit px) keeps
the contrast test from having to carry the whole defence.

## Configuration

```yaml
hole_detect:
  enabled: true
  # radius_px defaults to fiber_method_params.fiber_radius_px
  search_radius_px: 12.0
  min_snr: 6.0
  min_samples: 5
  max_scatter_px: 1.5
  refine_max_offset_px: 10.0
```

`radius_px` deserves attention: as a matched-filter template radius it is
a **precision constant** — 1 px of error costs 1.2–1.9 px of reported
position — whereas the same number as a solver dead-zone width tolerates
~20 % error harmlessly. Measure it on an illuminated or flat frame and set
it explicitly rather than inheriting the dead-zone value.

## Operator flow

1. Put a bright source on the reticle, or illuminate the entrance. Any
   mode, any method.
2. The `refine reticle` control enables itself once the measurement is
   stable, labelled with the offset it would apply; its tooltip always
   explains the current state. The magnifier draws the measured target
   next to the reticle, so the disagreement is visible before acting.
3. Click. The service re-validates against the freshest measurement — the
   button's state may be several frames old — and refuses with a reason
   rather than acting on stale evidence.
4. `central_point` moves. `central_point_default` does not, so
   reticle-home still returns to the configured value and accumulated
   drift stays visible.

Each refinement emits a `reticle_refined` event and a journal line with
old and new position, offset, SNR, sample count and scatter. Temperature
is deliberately absent: ambient and dome temperature are already archived
on the telemetry stream, so a drift model can join on timestamp instead of
the guider duplicating a sensor feed it does not own.

## Interfaces

- State: `hole_candidate` — `{x, y, offset_px, snr, scatter_px, samples,
  refinable, reason, ts_monotonic}` or `null`. `refinable` is the
  authoritative gate for offering the action.
- RPC: `refine_reticle`, no arguments. Returns `{status: "ok",
  central_point, offset_px, …}` or `{status: "error", error}`.
- Config: `hole_detect` block per pipeline.

## Measured behaviour

From `tests/unit/test_hole_detect.py`, on synthetic frames (Gaussian PSF
partly swallowed by a circular absorber, with sky, photon and read noise
plus row banding):

- Source on the entrance, reticle stale by 0–12 px: recovered to
  **0.1–0.6 px**.
- Source drifting off the entrance: usable to ~5 px separation, refused
  beyond, where the error becomes unpredictable rather than merely large
  (0.15 px at 10 px separation but 3.3 px at 15 px — the gate matters
  more than the trend).
- Saturated core: no degradation. Unlike centroid methods, a saturated
  plateau *helps* — maximum rim contrast.
- Uniform illumination: works, sub-pixel.
- Blank sky: never detected; the tracker stayed non-refinable across 20
  consecutive noise frames.
- Star entirely inside the hole (seeing much finer than the hole):
  refused, since no light escapes to reveal the entrance.
- Faint source: the gate tightens automatically, refusing a geometry at
  5 000 ADU peak that it accepts at 30 000.

## Alternatives considered

Recorded so the search is not repeated. The question was whether a
symmetry or moment statistic could hold the star on the entrance
*without* knowing the entrance position — making calibration irrelevant
rather than merely refreshable.

Such metrics do exist. The third central moment (skewness vector) of the
visible halo about its own flux centroid has its zero where the star sits
on the hole, independently of the configured reticle, and holds
0.03–0.04 px across an 8 px calibration error in moderate seeing. It is
nevertheless unsuitable as a control metric on three counts: capture range
of ≤8 px, falling to ~3 px once seeing exceeds the hole; a genuine sign
inversion at 5–7 px separation, which parks the star ~7 px off the
entrance in poor seeing even with the gain tuned correctly; and severe
sensitivity to a companion star — an equal-brightness neighbour 10 px away
costs over 2 mag, and such companions do occur inside the analysis window.
It remains attractive as a read-only diagnostic.

The variant comparing the flux centroid with a mirror-symmetry centre
fails outright: two competing symmetry branches make it diverge at every
seeing. Annulus dipoles about the assumed centre buy nothing on
calibration, since their zero is defined by the assumed centre.

Measuring the feature directly — this component — is what survives: flat
across calibration error, immune to companions, gradients, banding and
saturation, and it degrades by refusing rather than by lying.

Extremum seeking (dithering the mount and following coupled flux) is a
different class of solution, deliberately not pursued: it needs
calibration effort and mount motion that a measurement does not.

Full simulation study, including the negative results and the numbers
behind these claims, lives in the shared knowledge base
(`Architecture/Fibre guiding metrics - symmetry study`).

## Known limits

**A bias when the source is not centred.** The score cancels an additive
linear gradient exactly, but not the curvature of a halo across the
template. The residual is zero at zero star–hole separation and reaches
about −1.5 px at 1.5–5 px separation in tight seeing (FWHM 4–6; ≈−0.9 px
at FWHM 8, −0.16 px at FWHM 12), reversing sign beyond ~8 px. For the
manual action this is bounded and small, because the per-sector contrast
gate refuses badly off-centre sources anyway. It matters if the detector
is ever allowed to drive the solver continuously, where a systematic
target offset becomes a floor on the closed loop. A local quadratic
background halves it; a local likelihood-ratio score removes it.

**The sky-only channel is unused.** The excellent-seeing case is refused
as "no light", but sky background also outlines the entrance and the
deficit survives destriping as a relative disk, so the signature exists
in principle. Whether the deployed gates clear on real sky is an
empirical question. If they do, the detector would stop being "works when
someone sets it up" and become "always knows where the entrance is",
which is what would make continuous re-referencing safe.

**A dust mote is geometrically indistinguishable from the entrance** and
would pass the consistency test, being static too. The defences are the
bounded search radius, the `refine_max_offset_px` ceiling, and the
operator seeing the marker before clicking. Requiring deliberate
illumination is what actually disambiguates.

**Aim point and dead zone are coupled.** Measuring the entrance does not
by itself fix fibre guiding: a wide dead zone around a correct target is
still wide — and worse, with the legacy dead zone spanning the full hole
radius (≈3.4 px real), re-referencing the reticle onto a measured
entrance is a **no-op for few-pixel drifts**, because the loop keeps
classifying the offset as "centred". The fibre method therefore has a
`dead_zone_px` parameter that decouples the dead zone from the hole
radius (unset → legacy ramp; set → plain threshold + 1:1 with the
Enforcer's damping handling the photocentroid's over-report). Deploy the
two together; either alone leaves most of the coupling loss in place.
