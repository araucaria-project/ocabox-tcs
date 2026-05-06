# Guider service — architecture

**Status:** draft, planning phase. Bootstrapped from
[kickoff notes](000-kickoff-notes.md) and refined through discussion rounds
captured in [prior-art.md](prior-art.md). Open questions are flagged with **❓**.

## 1. Vision

A single TCS service (`guiding_svc.guider`) that runs all guiding for the
telescope. Internally it is a small fleet of consumer–producer pipelines. The
service decouples the **control plane** (commands, state, telemetry over NATS)
from the **data plane** (image acquisition → stacking → solving → corrections),
so each plane can evolve independently and other teams can wire integrations
against the control plane before the data plane is real.

## 2. Containment hierarchy

```
guider service                                 # one TCS service
├── CameraArrayCollector × N                          # one per camera (was "Collector")
│   ├── Backend (DownloaderRPC | DirectFetch | FitsWatch | Sim)
│   └── CameraOperator (priority queue, atomic exposure params)
└── Guider × N                                 # logical guider per camera
    ├── CameraInfo (LiveDocument-driven)
    └── Pipeline × M_i                         # M_i variants per camera
        ├── PipelineState (shared, Controller-mutated)
        └── Stage chain
            (frame source) → Stacker → Solver → (Controller, Enforcer)
```

**Invariants**

- **At most one** pipeline in `guiding` mode per camera. Other pipelines on the
  same camera may be in `monitoring` or `off`. Enforced by the Guider.
- Cameras independent — no cross-camera coordination at the guider level.
  Cross-camera arbitration (e.g., "use cam-A correction for mount") is handled
  by the mount client choosing one feed; out of scope here.
- Camera hardware ownership: only **CameraOperator** (per camera) talks to
  hardware/protocol. Pipelines submit `ExposureJob`s; CameraOperator schedules.

## 3. Data plane

### 3.1 CameraOperator — single hardware owner per camera

Per camera, one `CameraOperator` owns the protocol session and exposure
scheduling. Pipelines never touch hardware directly — they submit jobs.

```
                ┌────────────────────────────────────────┐
Pipelines ──▶   │       CameraOperator (per camera)      │  ──▶  Camera (hw)
ExposureJob     │  priority queue (with aging)           │
                │  current exposure params snapshot      │
                │  result delivery to requesting client  │
                └────────────────────────────────────────┘
```

**`ExposureJob` schema** (atomic exposure parameter set):

```python
@dataclass
class ExposureJob:
    pipeline_id: str                      # who submitted
    roi: tuple[int,int,int,int] | None    # (x, y, w, h) or None = full sensor
    exp_time: float
    gain: int | None
    binning: int | tuple[int,int]
    priority: int                         # base priority
    age_factor: float = 1.0                # priority gain per second waiting
    submitted_at_monotonic: float = 0
    deadline: float | None = None          # optional hard deadline
    # ... extensible per protocol (e.g., readout_mode for cameras that have it)

    def effective_priority(self, now: float) -> float:
        return self.priority + (now - self.submitted_at_monotonic) * self.age_factor
```

**Submit modes** (CameraOperator API):

| API | Use case |
|---|---|
| `submit_one(job) → Frame` | Snapshot, calib build, dark/bias build (single-shot) |
| `subscribe_stream(params, queue) → Subscription` (ordered) | Guiding/monitoring (continuous; params update-able while running) |
| `subscribe_opportunistic(criteria, queue) → Subscription` (future) | Live-view, low-priority consumers — see §3.1.2 |

`subscribe_stream` is "reservation of a slot in the camera cycle" — the operator
schedules subsequent jobs as time permits, parameters can be updated mid-stream
(without re-subscribing).

**Scheduling**: priority queue with aging. Effective priority = `priority + age *
age_factor`. Lower-priority jobs gain priority over time → no starvation.

**Coalescence** (future optimisation, not in initial frame): if multiple jobs
have **compatible parameters** (same ROI/exp/gain/binning) the operator can run
one exposure and deliver the result to all requesters. **Not implemented in
frame**; the abstraction has the slot.

**Naïve initial implementation**: FIFO, single subscriber dominates, ROI ignored
(always full sensor). Real scheduling lands when a second pipeline-on-camera
scenario appears.

#### 3.1.1 Camera settings cache (diff-and-apply)

The CameraOperator maintains a per-camera **mirror of last-applied settings** —
`gain`, `binning`, `roi`, `readout_mode`, `exp_time`, plus protocol-specific
extras (cooler setpoint, offset, gain-mode, …). Before each exposure it computes
a **diff** between the next `ExposureJob` and the cache and only sends the
changed fields to the camera over the wire.

Why this matters: each Alpaca `PUT /<setting>` is a 10–50 ms HTTP round-trip and
many guider/live-view profiles change only `exp_time` between frames. Naïvely
re-applying every parameter dominates the duty cycle; diff-apply makes
back-to-back exposures with stable parameters near-zero overhead.

```python
class SettingsCache:
    """1:1 mirror of camera state as the operator believes it to be."""
    state: dict[str, Any]            # "what is set on the camera right now"
    dirty: bool = False              # True if cache may be stale
    last_synced_at: float            # monotonic timestamp

    def diff(self, job: ExposureJob) -> dict[str, Any]:
        """Subset of (gain, binning, roi, exp_time, …) that differs."""

    def applied(self, fields: dict[str, Any]) -> None:
        """Mark fields as written; bump last_synced_at."""

    def invalidate(self, reason: str) -> None:
        """Force full re-apply on next exposure (after reconnect, error, etc.)."""
```

**Cache invalidation triggers** (force full re-apply):
- Backend reconnect (HTTP session drop, IRIS reconnect)
- Any `set_*` failure (state of camera unknown)
- `CameraOperator.notify_event("camera_reconfigured")` — external tool fiddled
  with the camera (e.g. operator used Alpaca dashboard)
- Optional periodic re-sync (configurable, default off — only on errors)

Cache lives in the **operator**, not in the protocol — the protocol is a
stateless wire format; the operator owns *"what does the camera actually have
set right now"* as a piece of authoritative process state.

**Constraint:** settings cache is meaningful only with `DirectFetchBackend`
(this process owns the camera session). `DownloaderRPCBackend` delegates the
session to a downloader process — settings are that process's concern there;
the operator's cache is bypassed. The backend should expose a
`supports_settings_cache: bool` capability so the operator can no-op cleanly.

#### 3.1.2 Subscription kinds: ordered vs opportunistic (future)

Two flavours of stream subscription:

| Kind | Semantics | Use case |
|---|---|---|
| **Ordered** | Pipeline pushes its own `ExposureJob`s to the queue; operator schedules; result delivered to that pipeline only. | `guiding`, `monitoring` — pipeline owns exact parameters and cadence |
| **Opportunistic** | Pipeline declares **acceptance criteria** (e.g. `binning ∈ {1, 2}`, `exp_time ≤ 0.5 s`, `roi_contains: (cx, cy)`, …). Every acquired frame is offered to matching opportunistic subscribers in addition to the requester. No own job needed; pipeline runs at "best-effort" cadence. | `live` — UI feed; low-priority telemetry consumers |

**Acceptance criteria** are a structured filter — first cut:

```python
@dataclass(frozen=True)
class AcceptanceCriteria:
    binning_in: tuple[int, ...] | None = None     # None = any
    gain_in: tuple[int, ...] | None = None
    exp_time_max: float | None = None
    exp_time_min: float | None = None
    roi_contains: tuple[int, int] | None = None    # (x, y) must be inside frame ROI
    accept_full_frame_only: bool = False
    # extensible — keep predicate-style so future fields don't break consumers
```

**Constraint:** opportunistic dispatch requires `DirectFetchBackend` — the
operator owns the frame buffer and can fan out cheaply to multiple in-process
consumers. `DownloaderRPCBackend` is request-response per caller; opportunistic
mode is unsupported there (the backend exposes
`supports_opportunistic: bool = False`; the operator routes such subscriptions
to ordered jobs at low priority instead, with a one-time warning).

**Ordering rule:** when an exposure completes, the operator delivers the frame
to the *requesting* subscription first (ordered), then offers a reference (no
copy — frames are immutable arrays) to all opportunistic subscribers whose
criteria match. Late or slow opportunistic consumers drop frames silently
(bounded queue full → discard, not block). Backpressure rule: opportunistic
subscribers **must not** stall the pipeline they're piggybacking on.

**Live-view as the canonical opportunistic consumer:**
- declares broad acceptance — "any frame, exp_time ≤ 0.5 s, full-frame
  preferred but ROI ≥ N px² acceptable";
- additionally pushes its own low-priority ordered jobs (`live` profile: high
  gain, full-frame, short exposure, no stacking, `priority = -10`) so when the
  camera is otherwise idle it produces dedicated live frames;
- when higher-priority pipelines are running, live-view consumes their frames
  opportunistically — "good enough" for human-in-the-loop UI even at
  non-live-view parameters.

### 3.2 Backends and Protocols (image source plug-ability)

CameraArrayCollector is parameterised by a **Backend**. Backends and protocols
are two orthogonal pluggability dimensions.

```
CollectorBackend (where do frames come from?)
├── DownloaderRPCBackend          # NATS RPC → oca-fits-proc downloader (deferred)
├── DirectFetchBackend            # this process talks to camera directly
│    └── CameraArrayProtocol      # protocol-pluggability inside DirectFetch
│        ├── AlpacaProtocol       # Alpaca HTTP + imagebytes binary
│        ├── IrisProtocol         # IRIS native protocol (different — not HTTP)
│        └── FileSimProtocol      # cam_sim — read .fits/.txt from disk
└── FitsWatchBackend              # watch directory for new files (downloader writes)
```

**Implementation order** (resolved E1):

| iteration | backend(s) | rationale |
|---|---|---|
| Frame (§8) | `SimBackend` only (`FileSimProtocol`) | Smoke tests; no real hw needed |
| Dummy (§9) | `SimBackend` with synthetic stars | Integration partners get realistic telemetry |
| First real | `DirectFetchBackend + AlpacaProtocol` | Minimal effort to first real frame (~200 LOC); guider cameras are typically separate from science cameras so single-client conflict doesn't apply |
| Later | `DownloaderRPCBackend` | When operationally motivated (e.g., centralised raw FITS storage). Requires new RPC verb in OFP downloader (current schema is FITS-pipeline-shaped, not "give me array now") — coordinate with Mirek when needed. |

**Protocol code lives in pyaraucaria** (E2 outcome) with optional extras —
`pyaraucaria[alpaca]` pulls `aiohttp`. Default `pip install pyaraucaria` stays
lean (no aiohttp dep for users who only need `FFS`, `fits`, `dome_eq`, etc.).
See [packaging-plan.md](packaging-plan.md).

### 3.3 Stacker — frame timing, integration, preprocessing

**Contract:** consumes `RawFrame`, produces `AnalysisFrame` with preprocessing
common to all Solver methods. Method-specific filters (Gaussian smoothing for
detection, cross-correlation reference image, etc.) live in Solver — see §3.4.

Responsibilities:
- Aligns to pipeline `frequency` setting (drops/queues frames if source runs at
  a different cadence)
- Stacks `stacking_count` raw frames → 1 analysis frame (`stacking_method ∈
  {median, mean, sum}` with optional `sigma_clip` wrapper, default `median, no
  clip`)
- Applies **preprocessing chain** (§3.3.2)
- Optionally writes raw FITS / stacked FITS / thumbnails per `save_*` flags

`stacking_count` and `corrections_avg_no` are **independent dimensions**:
- `stacking_count` (Stacker, **input side**): N raw frames → 1 analysis frame.
  Improves SNR per analysis frame.
- `corrections_avg_no` (Solver, **output side**): K solutions → 1 averaged
  correction. Smooths short-term jitter.

#### 3.3.1 Calibration (bias + dark_current scalable strategy)

**Physics model**:
```
pixel(t) = bias + dark_current * t + signal(t) + read_noise + photon_noise
```

`bias` — exposure-time-INDEPENDENT (electronics). `dark_current` — scales
linearly with `t` (thermal). Strategy 1 (recommended): build separate masters,
extrapolate to any exposure.

**Build**:
- `master_bias`: aggregate of N zero-time exposures. Stable (refresh weekly).
- `master_dark_current`: built from N exposures at long `reference_exp_time`
  (e.g. 60 s), bias-subtracted, then divided by `reference_exp_time` →
  e-/s/px map. Less stable (depends on temperature; refresh daily).

**Aggregation** (used for both master_bias and master_dark_current builds):

```yaml
build:
  n_frames: 7
  method: median            # 'median' | 'mean' | 'sum' (= mean for darks)
  sigma_clip:               # optional cosmic-ray rejection wrapper
    enabled: false
    sigma: 3.0
    iterations: 3
# Per-pixel: stack of N values → optional sigma-clip iterations →
# `method` (median/mean) on surviving values.
```

**Method trade-off**:

| situation | recommended | rationale |
|---|---|---|
| typical CCD, long darks, clean field | `median, no clip` | fast, naturally robust to outliers |
| short darks or low-signal detector | `mean, clip σ=3` | float precision, preserves sub-ADU; median aliases on integer values when range is narrow |
| suspect cosmic rays | `median, clip σ=3` | belt + suspenders (rare need) |
| fast rebuild without cosmics | `median, no clip` | <1 s on 4096² × 7 frames |

`mean + 3σ clip × 3 iter` is ~5–10× slower than median (one-time cost during
build, ~5–10 s vs ~1 s — acceptable). Backed by `ImagesStacking._sigma_clipping`
from OFP (post-extraction).

**Apply** (any actual exp_time):
```
clean = raw - master_bias - master_dark_current * actual_exp_time
```

**Total integration time matters more than n_frames** — DNR ∝
`dark_current * sqrt(N*T_ref) / sqrt(read_noise² + dark_current * T_ref)`.
Longer per-frame `T_ref` reduces read-noise contribution. Cosmic-ray
contamination at very long T → fall back to `sigma_clip`. Build config exposes
both `n_frames` and `reference_exp_time`; total = product.

**Does calibration improve SNR for star detection?** Strictly: no — uniform
subtraction of a constant doesn't change SNR. What it *does* improve:
- Removes **structured offsets** (hot pixels, banding) which inflate
  `q_sigma` in `find_stars` → fewer false detections, real stars not
  thresholded out.
- Stabilises detection threshold across detector temp swings.

**Implication**: calibration is **marginal for guider pipelines** (single bright
star, small ROI, hot pixels rarely interfere). Default `enabled: false` for
`guiding`/`fast-mon` modes. **Default `enabled: true` for `monitoring` pipelines
that do astrometry / multi-star** (full-frame, sensitive to false detections).

Fallback strategy `matched` (Mirek's pattern): per-exp-time master_dark
(absorbs bias). Less flexible but supports legacy workflows. Configurable.

#### 3.3.2 Preprocessing chain

Applied in order, each step optional/conditional. All apply to the stacked
analysis frame (post `stacking_method`):

1. **Bias subtraction** (§3.3.1) — `frame -= master_bias`
2. **Dark current subtraction** (§3.3.1) — `frame -= dark_current_map * exp_time`
3. **Flat field correction** *(deferred — needs new master)* — `frame /= master_flat`.
   Marginal for guiders (one star, small ROI). Useful for `monitoring`
   pipelines doing astrometry over full frame (vignetting affects multi-star
   matching). Same calibration mechanism as bias/dark (master + max_age + build
   triggers).
4. **Bad pixel mask** *(per camera, optional)* — static FITS mask loaded once at
   pipeline init from camera config. Bad pixels (hot/cold/dead/RTS) replaced
   with local 3×3 median, or marked NaN for downstream rejection. Loaded from
   per-camera file like `bad_pixels.fits`; if no file, step skipped.
5. **Saturation masking** *(always-on)* — pixels `>= saturation_threshold`
   (from CameraInfo, e.g., `2^bit_depth - margin`) masked as NaN. Solver
   methods know to ignore NaN in centroid math.

```yaml
preprocessing:
  bad_pixel_mask:
    enabled: false
    file: /mnt/data/calib/{tel_id}/{cam_id}/bad_pixels.fits
    replacement: local_median   # 'local_median' | 'nan'
  saturation:
    threshold: null              # null = derive from CameraInfo bit_depth
    margin: 100                  # subtract from max value as safety margin
```

Fallback strategy `matched` for the bias/dark step (Mirek's pattern):
per-exp-time master_dark absorbing bias. Less flexible but supports legacy
workflows. Configurable via `calibration.strategy`.

### 3.4 Solver — analysis and correction series

**Contract:** consumes `AnalysisFrame`, produces `Correction` and rolling
solution history.

Maintains:
- Reference state (`acquired_pos`, `acquired_adu`, optional reference image)
- Rolling history of last K solutions (configurable length)
- Per-frame `Correction` and averaged `Correction` (over `corrections_avg_no`
  via `corrections_avg_method ∈ {median, mean, weighted}`)

#### 3.4.1 Two pluggability dimensions

`Solver` is parameterised by **method × selection policy**:

- **Selection policy** = how to pick THE guide star from detected candidates
  (or whether to compute over a region without selecting)
- **Method** = how to compute correction once tracking is established

Different concerns; orthogonal except where they obviously interact.

**Selection policies**:

| policy | description | use case |
|---|---|---|
| `brightest_in_window` | first detection (by ADU desc) within `±search_reg_px` of `central_point` | typical guider with operator-pointed star (Mirek's default) |
| `brightest_in_adu_range` | first detection in `[min_adu, max_adu]` window, no position constraint | auto-pick when no operator hint (Mirek's auto fallback) |
| `closest_in_window` | closest detection to `central_point` (independent of ADU) | spectrograph: nearest star to fiber |
| `closest_excluding_zone` | closest detection outside an exclusion zone around `central_point` | spectrograph: science target falls fully into fiber, guide on neighbour |
| `weighted_score` | rank by `adu / (1 + dist²)` or similar | mixed cases needing tuning |

#### 3.4.2 Method catalogue

| method | input | uses_adu_match | produces_rotation | algorithm |
|---|---|---|---|---|
| `dummy` | any | — | no | random jitter, integration phase |
| `centroid` | 1 star in subraster | no | no | weighted centroid only |
| `single_star` | 1 star + ref pos | yes | no | Mirek's `GuidSimple` (subraster crop + ADU match) |
| `multi_star` | M stars | no | no | rigid-body translation, median Δ over M |
| `multi_star_affine` | M stars | no | yes | affine fit (translation + rotation) |
| `cross_correlation` | full subraster | no | no | image correlation with reference subraster |
| `image_diff` | image vs ref | no | no | difference + peak (planned) |
| `fiber_photocentroid` | residual flux in fiber zone | no | no | weighted COM at known fiber pos (no star detection) |
| `astrometry` | full image | no | yes | astrometric solve (slow, recovery use) |

`uses_adu_match`: method uses ADU-tolerance window for re-acquisition between
frames (subraster + ADU window). `produces_rotation`: method estimates rotation
in addition to translation.

#### 3.4.3 Acquired flag and re-acquisition

Solver tracks `acquired: bool` in `PipelineState`:

- `acquired = False` → method runs in "wide search" mode (full frame or `central_point ± wide_search_radius_px`). On first successful detection: store
  `acquired_pos`, `acquired_adu`, set `acquired = True`.
- `acquired = True` → method runs subraster crop around `acquired_pos`. On
  successful match: emit correction, update `acquired_pos`. On failed match
  (star_lost): set `acquired = False` (return to wide-search next frame).

Same code path covers cold start, calib (initial lock), and recovery from
star_lost. No explicit `calib` mode is required.

#### 3.4.4 ADU-tolerance normalisation

Methods that match by ADU need a tolerance window. ADU scales with exposure
time, so the tolerance is configured **per-second**:

```
adu_match_tolerance_per_sec: 5000   # window per 1 s exposure
# applied: tolerance = adu_match_tolerance_per_sec * cur_exp_time
# expected next ADU = prev_adu * (cur_exp / prev_exp)
# match if |detected_adu - expected_adu| < tolerance
```

**Caveat**: methods like `fiber_photocentroid` that rely on flux *suppression*
(perfect fiber-hole guiding → low residual ADU) cannot use ADU matching. The
`uses_adu_match: false` flag in the catalogue signals this.

#### 3.4.5 Method-internal preprocessing (detection vs centroid)

Stacker (§3.3) provides a calibrated, masked `AnalysisFrame`. Solver methods
typically apply *additional* internal preprocessing for the **detection**
step, but compute centroids on the unsmoothed (post-Stacker) data to preserve
sub-pixel accuracy.

**Pattern** (Mirek's `BaseGuid` + `pyaraucaria.ffs.FFS` use this):

```
AnalysisFrame
   ├─ (Gaussian-smoothed, threshold) → mask of local maxima → candidate (x,y)
   └─ (raw, around each candidate)   → centroid math (PCA / weighted COM)
```

Smoothing helps **detection** (raises peak SNR vs noise floor), centroid uses
the unsmoothed data so sub-pixel position is unbiased. Symmetric Gaussian
convolution is linear and analytically preserves centroid — but only when
applied to the *detection mask* generation, not to the data feeding the
centroid math.

**Per-method internal steps** (catalogue):

| step | methods using it | purpose |
|---|---|---|
| Gaussian smoothing | `centroid`, `single_star`, `multi_star`, `multi_star_affine` | detection mask SNR boost (FFS internal) |
| Local background subtraction | all centroid-based | subtract median of subraster border before COM |
| Saturation veto | all centroid-based | reject candidates with saturated peaks (already NaN-masked by Stacker) |
| FWHM check | all centroid-based | reject detections with FWHM ≫ typical (cosmic rays, satellite trails, resolved galaxies) |
| Single-frame cosmic veto | all centroid-based | cheap heuristic: pixel ADU > 10× neighborhood median = cosmic, mark NaN |
| Reference image | `cross_correlation`, `image_diff` | see §3.4.6 |
| Multi-frame matching | `multi_star`, `multi_star_affine` | match between frames by relative geometry, not ADU (robust against selection-policy ambiguity) |
| Reference catalog | `astrometry` | astrometric solve + WCS match |
| Drift prediction | all + auto-ROI | velocity from last K corrections → next-frame ROI center prediction |

These are **method-internal** — operator typically doesn't tune them, but each
method exposes its own knobs in `method_params`.

**Note on Gaussian smoothing default**: FFS already does this with `fwhm`
parameter (default 10 px in OFP). For guider with typical PSF FWHM ~3 px, a
matched-fwhm Gaussian raises detection SNR by ~3× without bias. Operator-tunable
via `method_params.fwhm`.

#### 3.4.6 Reference image freshness (cross_correlation, image_diff)

Methods that match against a stored reference frame need a freshness policy.
Reference becomes stale when:

- Focus changes (PSF FWHM shifts, correlation degrades)
- Filter changes (intensity normalisation breaks)
- Long-term drift (`acquired_pos` no longer matches reference center)
- Operator action (manual re-reference)

**Refresh triggers** (per pipeline, in Solver state):

- **Init**: capture reference on `acquired = False → True` transition
- **Manual**: RPC `set_reference()` command
- **Auto**: degraded match score over N consecutive frames → emit
  `reference_stale` event, optionally auto-refresh (configurable)

Reference held in Solver internal state (not in PipelineState — too big for
state mutation, snapshotted on init only).

#### 3.4.7 Auto-exposure (separate from ADU tolerance)

Independent control loop on `exp_time` to keep guide-star ADU in a target
window:

```yaml
auto_exposure:
  enabled: true
  target_adu_min: 25000
  target_adu_max: 45000
  exp_time_min: 0.1
  exp_time_max: 5.0
  step_factor: 1.3   # geometric step on adjustment
```

Different timescale from ADU matching (auto-exp ≈ exposure-level setpoint;
ADU-match ≈ frame-level ID window).

`adu_match_tolerance_per_sec` may default to a derived fraction of the
auto-exposure target range (e.g. `0.2 * (target_max - target_min)`) when both
are enabled.

### 3.5 Enforcer — apply corrections

**Contract:** consumes averaged `Correction(px)` (when authorised by Controller),
applies as pulse-guide on mount.

Steps:
1. Translate pixel → arcsec via camera `pixel_scale` (from `tic.config.observatory`).
2. Translate arcsec → pulse-guide seconds via per-mount **pulse-guide model**
   (RA gain, Dec gain, sign, deadband).
3. Issue pulse-guide commands via TIC RPC (`mount.aput_pulseguide(direction, ms)`).

**Correction payload** (from Solver):

```python
@dataclass
class Correction:
    dx_px: float
    dy_px: float
    drot_rad: float | None = None      # None when method doesn't measure
    method: str
    confidence: float
    n_stars_used: int | None = None
    fwhm_px: float | None = None
    timestamp: list[int]
```

**Rotation handling**: most OCM mounts have no rotator. Enforcer **applies only
`dx_px`/`dy_px`**. `drot_rad` is **observed and reported** (events/journal/
telemetry) for downstream consumers (pointing model builders) but not corrected.

**Pulse-guide model is itself an auto-adjusting parametric transformation**
— the (dx_px, dy_px, Alt, Az, Rot) → (dtA, dtB) mapping needs to be
calibrated initially and refined online. This is a recurring pattern in OCM
(also pointing model, focuser, exposure calculator, OB time calculator).
Treated as a separate concern in [`doc/auto-adjust-transformation.md`](../auto-adjust-transformation.md):
recommended hybrid is **active calibration bootstrap + RLS online
refinement with forgetting factor** (Stage A + Stage B in that doc §7).
Implementation lives in `pulse_guide.py` per the staging plan in §9 there.

❓ Pulse-guide model storage: ocadb (durable, queriable) vs config file (simple)
vs `tic.config.observatory` LiveDocument? Recommendation: try
`tic.config.observatory` first (consistent with other telescope config); fall
back to YAML if schema doesn't fit.

### 3.6 Control flow within a pipeline

Three concurrent flows, synchronised through shared `PipelineState`:

1. **Frame flow (down, hot path)**: `CameraOperator → AnalysisFrame → Stacker
   → Solver → Correction`
2. **Control flow (down, on-change)**: `Controller commands → PipelineState
   mutation → stages reconfigure on next iteration`
3. **Feedback flow (up/cross, dynamic)**:
   - `Solver → PipelineState (acquired_pos, current_exp_time, …) →
     CameraOperator next ExposureJob`
   - `Solver → Correction → Enforcer (when mode = guiding)`

**`PipelineState` is the synchronisation point.** Mutated only by Controller
(holds `asyncio.Lock`); read by stages via atomic snapshot copy at start of
each iteration. `version` field bumps on every mutation; stages can detect
state changes between iterations.

**Auto-* policies route through Controller.** Solver doesn't mutate
`PipelineState` directly — it submits suggested changes to Controller, which
arbitrates (e.g., operator-set mode wins over auto-recovery), validates, and
applies atomically. Prevents race conditions and centralises state authority.

## 4. Control plane — Controller

### 4.1 PipelineState (full schema)

| Group | Field | Type | Mutator | Notes |
|---|---|---|---|---|
| **Operator** | `mode` | enum | Controller | `off / monitoring / guiding / live` (`live` future, §3.1.2) |
| | `selection_policy` | str | Controller | catalogue §3.4.1 |
| | `method` | str | Controller | catalogue §3.4.2 |
| | `method_params` | dict | Controller | method-specific |
| | `exp_time` | seconds | Controller | nominal; may be overridden by auto-exp |
| | `binning` | int / NxN | Controller | |
| | `gain` | int | Controller | |
| | `frequency` | Hz | Controller | analysis frame rate target |
| | `central_point` | (px,px) | Controller | reference target / fiber position |
| | `wide_search_radius_px` | int | Controller | for `acquired=False` |
| | `search_reg_px` | int | Controller | subraster half-width when `acquired=True` |
| | `stacking_count` | int | Controller | |
| | `stacking_method` | enum | Controller | `median / mean / sum / sigma_clip` |
| | `corrections_avg_no` | int | Controller | |
| | `corrections_avg_method` | enum | Controller | `median` (default) / `mean` / `weighted` |
| | `adu_match_tolerance_per_sec` | float | Controller | normalised; null = derive from auto_exp |
| | `auto_exposure` | obj | Controller | `{enabled, target_adu_min, target_adu_max, exp_time_min, exp_time_max, step_factor}` |
| | `roi` | obj | Controller | `{enabled, margin_px, recenter_when_drift_frac, full_frame_on_lost}` |
| | `calibration` | obj | Controller | `{strategy, bias{...}, dark_current{...}}` (see §3.3.1) |
| | `save_raw_fits` | bool | Controller | |
| | `save_stacked_fits` | bool | Controller | |
| | `save_raw_thumbnails` | bool | Controller | |
| | `save_stacked_thumbnails` | bool | Controller | |
| **Auto** | `acquired` | bool | Controller (via Solver req) | lock state |
| | `acquired_pos` | (px,px) \| None | Controller (via Solver req) | last known star position |
| | `acquired_adu` | float \| None | Controller (via Solver req) | last known ADU |
| | `acquired_at` | iso ts | Controller (via Solver req) | for age display |
| | `current_exp_time` | float | Controller (via Solver req) | may differ from `exp_time` if auto-exp on |
| | `current_roi` | (x,y,w,h) \| None | Controller (via Solver req) | current camera ROI |
| **Observed** | `last_correction` | Correction \| None | Solver (publish) | latest emitted |
| | `fwhm_recent` | float \| None | Solver (publish) | smoothed |
| | `rotation_recent` | float \| None | Solver (publish) | when method measures |
| **Meta** | `version` | int | Controller | bumped every mutation |

### 4.2 CameraInfo (per camera, read-only)

Sourced from `tic.config.observatory` via `serverish.messenger.get_live_document`.
Auto-updates on republish (e.g., binning change → pixel_scale change).

```python
self.obs_cfg = await get_live_document('tic.config.observatory')
pixel_scale = self.obs_cfg.telescopes[tel_id].cameras[cam_id].pixel_scale
```

Published to `guider.camera.<cam_id>.info` on (a) startup, (b) underlying
LiveDocument change. No timer-based republish.

Fields (consumed):
- `resolution: (W, H)`
- `pixel_scale: arcsec/px`
- `model: str`
- `bit_depth: int`
- `gain_table: list` (optional)
- `exposure_min/max: seconds`
- `roi_supported: bool`

Per-camera YAML overrides allowed for fields TIC doesn't publish.

### 4.3 Telemetry split: events vs journal

Two channels with different semantics:

- **Events** (`...events`) — structured, machine-consumable. Schema-validated
  payloads. Examples: `image_saved`, `correction_applied`, `mode_changed`,
  `acquired_lost`, `acquired_gained`, `dark_rebuilt`, `roi_changed`,
  `arbitration_loss`. Consumed by dashboards, pointing-model builders, etc.

- **Journal** (`...journal`) — human-readable, free-form. Published via
  `serverish.messenger.MsgJournalPublisher`. Examples: "Building master dark at
  60 s, 7 frames", "Star lost, returning to wide search", "Operator override:
  pipeline `fast-mon` to monitoring". Consumed by operator UI, Halina AI.

A single occurrence may publish to both (typical pattern: structured event +
human-readable journal note).

### 4.4 Commands

Command convention:
- **RPC** (request-response) for state queries and mutations needing
  acknowledgement
- **Publish-forget** for fire-and-forget operations (less critical)

| Command | Type | Description |
|---|---|---|
| `set_state(patch)` | RPC | Atomic partial state update; validates, mutates, bumps version |
| `set_mode(mode)` | RPC | Convenience for state.mode change (subject to arbitration) |
| `acquire()` | RPC | Force `acquired=False` → next frame triggers wide search. Useful for re-lock without mode toggle. |
| `snapshot(params)` | RPC | One-off frame with parameters overriding pipeline state for that frame; returns frame metadata + image path |
| `dark_rebuild(params)` | RPC | Trigger master_dark_current build; long-running, returns task id |
| `bias_rebuild(params)` | RPC | Trigger master_bias build; long-running, returns task id |
| `calibrate_pulse_guide()` | RPC | Refine enforcer model (future; placeholder slot) |

## 5. NATS subject map

Service framework subjects (existing TCS convention, prefix `svc`):
```
svc.status.guiding_svc.guider.<instance>
svc.heartbeat.guiding_svc.guider.<instance>
svc.registry.{declared,start,stop,…}.guiding_svc.guider.<instance>
```

Guider data-plane subjects (separate top-level `guider.…` for clarity, matches
`tic.command.>` precedent):

```
guider.camera.<cam_id>.info                            # CameraInfo (retained)

guider.camera.<cam_id>.pipeline.<pipe_id>.state        # PipelineState (retained)
guider.camera.<cam_id>.pipeline.<pipe_id>.correction   # corrections (rolling)
guider.camera.<cam_id>.pipeline.<pipe_id>.stats        # solver/stage stats
guider.camera.<cam_id>.pipeline.<pipe_id>.events       # structured events
guider.camera.<cam_id>.pipeline.<pipe_id>.journal      # human-readable journal

guider.camera.<cam_id>.pipeline.<pipe_id>.cmd.<command>      # publish-forget
guider.camera.<cam_id>.pipeline.<pipe_id>.rpc.v1.<command>   # RPC

guider.camera.<cam_id>.active.correction               # convenience: live correction
                                                        # from currently-guiding
                                                        # pipeline (same payload)
```

Convenience `…active.correction` republishes the guiding pipeline's correction
under a per-camera stable subject so consumers don't have to track pipeline
IDs. When no pipeline is in `guiding` mode: subject silent.

## 6. Configuration

Single TCS service config block. Cameras and pipelines declared in YAML;
calibration and other operational parameters merge from defaults.

```yaml
services:
  - type: guiding_svc.guider
    variant: prod
    telescope_id: jk15

    # Optional service-wide defaults (all overridable per pipeline)
    defaults:
      stacking_method: median
      corrections_avg_method: median
      adu_match_tolerance_per_sec: 5000
      calibration:
        strategy: scaled              # 'scaled' | 'matched'
        bias:
          enabled: false              # default off for guiding pipelines
          folder: /mnt/data/calib/{tel_id}/{cam_id}/bias
          max_age_h: 168
          build:
            n_frames: 16
            method: median            # 'median' | 'mean' | 'sum'
            sigma_clip:
              enabled: false
              sigma: 3.0
              iterations: 3
        dark_current:
          enabled: false
          folder: /mnt/data/calib/{tel_id}/{cam_id}/dark_current
          max_age_h: 24
          reference_exp_time: 60.0
          build:
            n_frames: 7
            method: median
            sigma_clip:
              enabled: false
              sigma: 3.0
              iterations: 3

    cameras:
      - id: cam-A
        backend:
          type: downloader_rpc
          rpc_subject: tic.rpc.{tel_id}.cam-a.fetch.v1
          # Backend-specific fields below
        pipelines:
          - id: guide
            mode: off                  # operator opts in
            method: single_star
            selection_policy: brightest_in_window
            exp_time: 2.0
            binning: 1
            stacking_count: 4
            corrections_avg_no: 5
            central_point: [1024, 1024]
            search_reg_px: 25
            wide_search_radius_px: 200
            auto_exposure:
              enabled: true
              target_adu_min: 25000
              target_adu_max: 45000
              exp_time_min: 0.5
              exp_time_max: 5.0
            roi:
              enabled: false           # frame-only initially (see §3.1)
              margin_px: 100
            save_raw_fits: false
            save_stacked_thumbnails: true

          - id: monitor
            mode: monitoring
            method: multi_star_affine
            selection_policy: brightest_in_adu_range
            exp_time: 2.0
            stacking_count: 1
            corrections_avg_no: 3
            calibration:                # override service defaults
              bias: { enabled: true }
              dark_current: { enabled: true }
            save_stacked_fits: true

      - id: cam-B
        backend:
          type: direct_fetch
          protocol:
            type: alpaca
            url: http://alpaca.oca.lan:11111
            device_id: 0
        pipelines:
          - id: sim
            mode: off
            method: dummy
            exp_time: 1.0

      - id: cam-sim
        backend:
          type: direct_fetch
          protocol:
            type: file_sim
            files: ["/mnt/data/sim/cam-A/frame-*.fits"]
            interval_s: 1.0
        pipelines:
          - id: dev
            mode: monitoring
            method: single_star
```

Bias/dark builds have a `frequency: per_camera | per_pipeline` knob (defer;
default `per_camera`).

## 7. Lifecycle and process model

- **Service** runs as a TCS `BasePermanentService` (non-blocking).
- On `on_start`:
  1. Open `serverish.messenger.get_live_document('tic.config.observatory')` →
     hold reference.
  2. Initialise `ocaboxapi.Observatory` → handles for `Telescope` and `Mount` per
     camera.
  3. Construct `CameraArrayCollector × N` (one per camera; each builds its
     `CameraOperator` and Backend per config).
  4. Construct `Guider × N` (logical), each with `Pipeline × M_i` configured
     pipelines.
  5. For each pipeline: create `PipelineState` (from YAML), `Stacker`, `Solver`,
     `Enforcer`. Wire stage queues.
  6. Subscribe pipelines to their `CameraOperator` (via `subscribe_stream` for
     `monitoring`/`guiding` pipelines; `off` pipelines don't subscribe).
  7. Publish initial `CameraInfo` per camera. Hook
     `tic.config.observatory.on_change` to re-publish on changes.
  8. Register RPC handlers per pipeline (`set_state`, `set_mode`, `acquire`,
     `snapshot`, `dark_rebuild`, `bias_rebuild`).
- On `on_stop`: stop subscriptions, drain queues, close
  `CameraOperator`s, deregister.

### 7.1 Concurrency model

- One asyncio loop per service.
- Each pipeline = small task tree (`Stacker`, `Solver`, `Enforcer` coroutines).
- Each `CameraOperator` = one scheduling coroutine + one execution coroutine.
- Stages connected by **bounded** `asyncio.Queue` (size from config, e.g. 4).
- Backpressure: a slow stage stalls upstream queue rather than ballooning
  memory.
- CPU-heavy work in solver methods (large frames, FFT for cross-correlation,
  astrometric solve): `asyncio.to_thread` per-frame. Not subprocess-isolated
  initially — defer until a real method needs it.

### 7.2 PipelineState locking

```python
class PipelineState:
    _lock: asyncio.Lock
    _data: dict
    version: int

    async def update(self, patch: dict) -> int:    # Controller-only
        async with self._lock:
            self._data.update(patch)
            self.version += 1
            return self.version

    def snapshot(self) -> tuple[dict, int]:        # lock-free read
        return copy.deepcopy(self._data), self.version
```

Stages call `snapshot()` at the start of each iteration; if `version` changed
since last iteration, they reconfigure. Reads are lock-free (deep copy under
the GIL is atomic for our purposes).

## 8. Frame / shims (next iteration)

Goal: service starts, publishes registry/status/heartbeat/CameraInfo/
PipelineState; commands flip mode and update state; **no real images flow**.
Enough for integration partners (UIs, Halina, mount client) to wire against.

Concretely:

- Empty/skeleton `Guider`, `Pipeline`, `Stacker`, `Solver`, `Enforcer`,
  `CameraOperator`, `Backend` classes with the contracts above
- Bounded `asyncio.Queue` glue between stages
- Controller skeleton with `PipelineState`, NATS subject wiring, stub command
  handlers (return ack, mutate state, publish event)
- `CameraInfo` publisher tied to LiveDocument
- One concrete `Backend`: **`SimBackend`** (file-sim) producing canned frames
  at configured cadence — enough for end-to-end smoke tests
- One concrete `Solver` method: **`dummy`** returning fixed/random corrections

No calibration math, no real camera, no real solving, no enforcer beyond
logging.

## 9. Dummy implementation (iteration after that)

Add minimal flesh to be observable end-to-end:

- `SimBackend` enriched: synthetic frames with injected star pattern (random
  jitter + drift) at configurable rate
- Stacker with naïve median stacking (no calibration)
- Solver method `centroid` (real arithmetic on simulated star)
- Enforcer logs pulse-guide commands instead of issuing them
- All command handlers fully wired

Unblocks integration partners with realistic-looking telemetry.

## 10. Out of scope (initial milestones)

- Real solver methods beyond `dummy` and `centroid`
- Pulse-guide model calibration logic
- Production `Backend` implementations (`DownloaderRPCBackend`,
  `DirectFetchBackend + AlpacaProtocol`)
- Master bias / dark builders (use config flag `enabled: false` until ready)
- Persisted run history / observability beyond NATS telemetry
- Multi-mount arbitration
- ROI auto-management (slot exists, default `roi.enabled: false`)
- ExposureJob coalescence (slot exists)
- ExposureJob aging policy (slot exists, FIFO initial)
- Settings cache diff-and-apply (§3.1.1) — slot exists in `CameraOperator`,
  initial impl re-applies all fields each exposure
- Opportunistic subscriptions and `live` pipeline mode (§3.1.2 / §4.1) —
  scaffolding (acceptance criteria, `subscribe_opportunistic`,
  `supports_opportunistic` capability) is in the operator/backend contracts
  from day one; no concrete consumer until live-view ships
- Live-view UI feed format and downsampling/encoding (NATS object store vs
  inline JPEG vs raw fanout) — pick when first UI consumer is on the table

## 11. Decisions

Resolved through kickoff + B/C/D rounds (see prior-art.md and conversation
follow-ups). Open items at the bottom.

**Resolved**:

| # | Decision | Outcome |
|---|---|---|
| 1 | Per-camera arbitration owner | Guider (one per camera) |
| 2 | Frame source cardinality | One `CameraOperator` per camera; pipelines submit jobs (not fan-out) |
| 3 | Calibration location | Stacker stage |
| 5 | NATS top-level | `guider.…` (separate from `svc.…`) |
| 6 | Per-camera "active correction" duplicate publish | Yes — `guider.camera.<id>.active.correction` |
| 7 | Subprocess isolation for heavy methods | Defer; `asyncio.to_thread` initially |
| 8 | Shared vs per-pipeline calibration config | Service `defaults`, per-pipeline override |
| 9 | Dependency strategy for OFP utilities | **All to pyaraucaria** — pure math/astro AND protocol code (Alpaca etc.) live in pyaraucaria with optional `[alpaca]` extras for new transitive deps (aiohttp). ocaboxapi NOT used for image fetch (it wraps TIC, not Alpaca). `oca-fits-proc` is NATS RPC peer only (DownloaderRPCBackend), no Python import. See [packaging-plan.md](packaging-plan.md). |
| 10 | Events vs journal | **Yes split** — `…events` structured + `…journal` human |
| 13 | Calib mode | Internal `acquired: bool` flag, automatic transitions; no explicit `calib` pipeline mode |
| 14 | CameraInfo source | `serverish.messenger.get_live_document('tic.config.observatory')` |
| — | Master dark/bias strategy | Bias + dark_current scalable (default), exp-matched (fallback) |
| — | Calibration default for guider | `enabled: false` (marginal benefit); `enabled: true` for `monitoring` |
| — | Total exposure principle for darks | Long `reference_exp_time` preferred over many short frames; total_integration = N × T_ref |
| — | `Correction` rotation handling | Reported in payload, **applied** only as `(dx, dy)` translation by Enforcer |
| — | ADU-tolerance scaling | Per-second normalisation: `tolerance = adu_match_tolerance_per_sec * exp_time` |
| — | Method/selection-policy split | Two orthogonal pluggability dimensions |
| — | OCM fetch model | **Locked**: Sim (frame/dummy) → **DirectFetchBackend + AlpacaProtocol** (first real, ~200 LOC, 2-3 days, aligned with team intuition) → DownloaderRPC (later when operationally motivated; currently OFP downloader has no array-only RPC verb) |
| — | Camera Array Collector naming | `CameraArrayCollector` (per-camera, owns hardware) with `CollectorBackend` (where) × `CameraArrayProtocol` (wire format) plug-ins |
| — | Calibration aggregation | `method ∈ {median, mean, sum}` with optional `sigma_clip` wrapper (sigma + iterations). Default `median, no clip` for typical CCD; `mean + clip` for low-signal (median aliases on small int range) |
| — | Camera settings management | Per-camera 1:1 settings cache in `CameraOperator` with diff-and-apply before each exposure (§3.1.1). DirectFetch only — RPC backend exposes `supports_settings_cache: false` |
| — | Frame dispatch model | Two subscription kinds: ordered (own jobs, dedicated frames) and opportunistic (acceptance criteria, piggyback on others' frames) (§3.1.2). Live-view is the canonical opportunistic consumer + low-priority ordered fallback. DirectFetch only — RPC backend exposes `supports_opportunistic: false` |
| — | Pipeline mode catalogue | `off / monitoring / guiding / live` — `live` is future-tracked (no stacking, no calibration, broad acceptance criteria, low-priority ordered fallback) |
| — | Preprocessing pattern | Stacker = method-agnostic (bias/dark/flat/bad-pixel/saturation); Solver = method-internal (Gaussian smoothing for **detection only**, centroid on unsmoothed post-calibration data). Reference frames for `cross_correlation`/`image_diff` held in Solver internal state with manual + auto refresh triggers. |

**Open** (not blocking the next iteration):

| # | Decision | State |
|---|---|---|
| 4 | Pulse-guide model storage | `tic.config.observatory` first; YAML fallback if schema doesn't fit |
| 11 | Master-dark/bias rebuild trigger semantics | Operator command (RPC) initial; auto-at-twilight later |
| 12 | Solver method catalogue completeness | Catalogue in §3.4.2 is starting set; extend per need |
| — | ROI auto-management defaults (`margin_px`, `recenter_when_drift_frac`) | Tune when first real pipeline runs |
| — | ExposureJob aging-policy parameters (`age_factor`, base priority) | Defaults TBD when 2nd pipeline-on-camera scenario lands |
| — | Coalescence rules (parameter equality predicate) | Defer |
| — | Flat field master implementation | Same calibration mechanism as bias/dark; defer until first user (likely `monitoring` pipeline doing astrometry) |
| — | Bad pixel mask source | Per-camera FITS file path in config; format TBD (boolean mask? coordinate list?). Defer until detector with known defects in production. |
| — | Reference image auto-refresh threshold (degraded-match policy) | Defer — needs operational data to pick the right metric |
| — | Live-view feed encoding | Raw fanout vs downsampled JPEG/PNG vs NATS object store. Bandwidth/latency trade-off; defer until first UI consumer concretises |
| — | Acceptance-criteria predicate richness | First cut: binning/gain/exp_time/ROI fields. Extend with method-specific hints (e.g. `psf_fwhm_max` for star-finder consumers) when a real opportunistic consumer beyond live-view emerges |

## 12. Reuse from prior art

Detailed analysis in [**prior-art.md**](prior-art.md). Packaging plan for
sharing utility code with OFP / pyaraucaria / ocaboxapi in
[**packaging-plan.md**](packaging-plan.md).

**Algorithms** ported (the math, not the code) from `BaseGuid` (master):
`find_stars` parametrisation, ADU-tolerance subraster re-acquisition,
ambiguous-match rejection, single-star selection by operator hint or ADU
window.

**Service shell pattern** adapted from `dome_follower_svc`: thin `@service`
entry + heavy `manager.py`. Stops being sufficient at per-pipeline stage
queues — those follow §3 design.

**Libraries** consumed:
- `pyaraucaria.ffs.FFS` — star finder (already there)
- `pyaraucaria.images_stacking.ImagesStacking` — Stacker math + master builders
  (post-extraction from OFP — see packaging-plan.md)
- `pyaraucaria` subset of `astro_tools` — `image_stretch_display`,
  `calc_pointing_error`, `ra_dec_to_pix`, `mark_stars`, `basic_stat_exec`
  (post-extraction)
- `pyaraucaria.alpaca_image` (new, `[alpaca]` extra → `aiohttp`) — Alpaca
  imagebytes binary client for `DirectFetchBackend + AlpacaProtocol`
- `ocaboxapi.Mount/Telescope/Dome` — TIC RPC handles (used for pulse-guide,
  observatory operations); **not** for image fetch (ocaboxapi wraps TIC, not
  Alpaca)
- `serverish.messenger.get_live_document` — `tic.config.observatory` access
- `serverish.messenger.MsgJournalPublisher` — operator journal channel
- `oca-fits-proc` — **only as RPC peer** (DownloaderRPCBackend); not imported
