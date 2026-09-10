"""PipelineState — shared mutable state for one pipeline.

Single point of truth for runtime state per pipeline. **Mutated only by
Controller** (via `update()` under `_lock`); read by stages via
`snapshot()` (lock-free deep copy).

"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from ocabox_tcs.services.guiding_svc.hole_detect import HoleDetectConfig


class Mode(StrEnum):
    OFF = "off"
    MONITORING = "monitoring"
    GUIDING = "guiding"
    LIVE = "live"  # future


class FramePhase(StrEnum):
    """Per-frame phase relative to the most recent issued pulse.

    Derived in the Solver from ``frame.t_mid_utc`` (mid-exposure
    timestamp) against ``active_pulse.{issued, motion_end, settled}_utc``.
    Drives stage behaviour:

    - ``TRACKING``  → no pulse in flight (or frame captured before it).
                      Run normal narrow-search around ``acquired_pos``.
    - ``IN_FLIGHT`` → mount is moving during this frame's exposure.
                      Star is smeared along the trajectory. Skip
                      detect; hold lock state; GUI draws an arrow
                      from ``src_pos`` to ``predicted_pos``.
    - ``SETTLING``  → motion complete, damping in progress. Skip
                      detect (frame may catch residual oscillation).
    - ``ACQUIRING`` → first frame whose mid-exposure is past
                      ``settled_utc``. Star should be near
                      ``predicted_pos``. Detect with a bracket-box
                      around the trajectory. On hit, lock and clear
                      ``active_pulse`` — pipeline returns to TRACKING.
                      After N consecutive ACQUIRING misses we give up
                      and demote to wide-search.
    """
    TRACKING = "tracking"
    IN_FLIGHT = "in_flight"
    SETTLING = "settling"
    ACQUIRING = "acquiring"


def classify_frame_phase(active_pulse: Any, t_mid: Any) -> "FramePhase":
    """Compute the phase of a frame.

    ``active_pulse`` is either a ``PulseEvent`` instance or its
    ``asdict`` projection (Solver works with the dict snapshot).
    ``None`` ⇔ no pulse in flight ⇔ ``TRACKING``.

    ``t_mid`` is a Python ``datetime`` in UTC — mid-exposure timestamp
    derived in Solver as ``dt_from_array(frame.timestamp) +
    exp_time/2``. We accept ``None`` defensively (returns TRACKING)
    so a malformed frame timestamp never blows up phase classification
    upstream.
    """
    if active_pulse is None or t_mid is None:
        return FramePhase.TRACKING
    # Tolerate both dict (from to_dict) and dataclass forms.
    if isinstance(active_pulse, dict):
        issued = active_pulse.get("issued_utc")
        motion_end = active_pulse.get("motion_end_utc")
        settled = active_pulse.get("settled_utc")
    else:
        issued = getattr(active_pulse, "issued_utc", None)
        motion_end = getattr(active_pulse, "motion_end_utc", None)
        settled = getattr(active_pulse, "settled_utc", None)
    if not (issued and motion_end and settled):
        return FramePhase.TRACKING
    # Lazy import keeps ``state`` lightweight for non-async callers.
    from serverish.base import dt_from_array  # noqa: PLC0415
    try:
        issued_dt = dt_from_array(issued)
        motion_end_dt = dt_from_array(motion_end)
        settled_dt = dt_from_array(settled)
    except Exception:  # noqa: BLE001 — malformed timestamp → safe default
        return FramePhase.TRACKING
    if t_mid < issued_dt:
        return FramePhase.TRACKING
    if t_mid < motion_end_dt:
        return FramePhase.IN_FLIGHT
    if t_mid < settled_dt:
        return FramePhase.SETTLING
    return FramePhase.ACQUIRING


# ---------------------------------------------------------------------------
# Sub-config dataclasses (operator-tunable)
# ---------------------------------------------------------------------------


@dataclass
class AutoExposureConfig:
    enabled: bool = False
    target_adu_min: float = 25_000
    target_adu_max: float = 45_000
    exp_time_min: float = 0.1
    exp_time_max: float = 5.0
    step_factor: float = 1.3


@dataclass
class ROIConfig:
    enabled: bool = False
    margin_px: int = 64
    recenter_when_drift_frac: float = 0.5
    full_frame_on_lost: bool = True


@dataclass
class CalibrationBuildConfig:
    n_frames: int = 7
    method: str = "median"  # 'median' | 'mean' | 'sum'
    sigma_clip_enabled: bool = False
    sigma_clip_sigma: float = 3.0
    sigma_clip_iterations: int = 3


@dataclass
class CalibrationStepConfig:
    enabled: bool = False
    folder: str = ""
    max_age_h: float = 24.0
    build: CalibrationBuildConfig = field(default_factory=CalibrationBuildConfig)


@dataclass
class CalibrationConfig:
    strategy: str = "scaled"  # 'scaled' | 'matched'
    bias: CalibrationStepConfig = field(default_factory=CalibrationStepConfig)
    dark_current: CalibrationStepConfig = field(
        default_factory=lambda: CalibrationStepConfig(
            build=CalibrationBuildConfig(n_frames=7),
        )
    )


@dataclass
class PreprocessingConfig:
    bad_pixel_mask_enabled: bool = False
    bad_pixel_mask_file: str = ""
    bad_pixel_replacement: str = "local_median"  # 'local_median' | 'nan'
    saturation_threshold: float | None = None  # None = derive from CameraInfo
    saturation_margin: float = 100.0


# ---------------------------------------------------------------------------
# Pulse event — first-class temporal record of an issued pulse
# ---------------------------------------------------------------------------


@dataclass
class PulseEvent:
    """Single source of truth for "a pulse has been issued and the mount
    is moving". Replaces the implicit triple of (Enforcer's monotonic
    cooldown gate, ``predicted_pos`` field, narrow-miss budget grace)
    with explicit absolute UTC timestamps that any stage can interpret.

    The lifecycle is:

    - Enforcer writes a ``PulseEvent`` to ``PipelineState.active_pulse``
      immediately after issuing pulse-guide commands. The three
      timestamps mark the boundaries of the four phases:

          ``t < issued_utc``        →  TRACKING (pre-pulse, normal)
          ``issued_utc..motion_end`` →  IN_FLIGHT (mount moving, smear)
          ``motion_end..settled``    →  SETTLING (mount damping)
          ``t >= settled_utc``       →  ACQUIRING (first valid frame)

    - Solver classifies each frame by ``frame.t_mid`` (mid-exposure UTC)
      against these boundaries and chooses behaviour per phase (skip
      detect during IN_FLIGHT/SETTLING, bracket-search during ACQUIRING,
      narrow-track during TRACKING).

    - Controller clears ``active_pulse`` on the first successful
      ``ACQUIRING``-phase acquire (predicted reached, lock re-latched).

    All timestamps are serverish 7-int UTC arrays (``[Y, M, D, h, m, s,
    μs]``) so they cross NATS unmodified and are directly comparable
    with ``frame.timestamp`` set by the camera protocol.
    """

    issued_utc: list[int]
    """When the enforcer commanded the pulse(s). Approximate within the
    duration of the ``aput_pulseguide`` round-trips (logged as
    ``wire_ms`` already)."""

    motion_end_utc: list[int]
    """When the mount is expected to finish executing the pulse —
    ``issued_utc + sum_of_active_durations_ms``. Pulses are issued
    sequentially per ASCOM, so total motion time is the *sum* of the N
    and E durations (after damping + clipping), not the max."""

    settled_utc: list[int]
    """When the mount damping is expected to be done —
    ``motion_end_utc + post_pulse_settle_ms``. After this any frame
    whose mid-exposure falls past ``settled_utc`` should reflect the
    new optical state and is the first one we trust to re-acquire."""

    src_pos: tuple[float, float]
    """The ``acquired_pos`` snapshot at issue time. Defines one endpoint
    of the trajectory drawn on the GUI (motion-blur oval / arrow) for
    frames classified as IN_FLIGHT."""

    predicted_pos: tuple[float, float]
    """Forward-Jacobian estimate of where the star ends up after the
    pulse: ``src_pos + J · (t_N_actual, t_E_actual)``. The other
    endpoint of the GUI trajectory and the search-box centre during
    ACQUIRING."""

    pulse_t_n_ms: float
    """Signed N-axis pulse duration after damping + clipping (positive
    = N, negative = S). Stored signed so downstream code can derive
    direction without re-running the sign-extraction."""

    pulse_t_e_ms: float
    """Signed E-axis pulse duration (positive = E, negative = W)."""

    correction_dx_px: float
    """The error vector this pulse was meant to cancel — useful for
    after-the-fact validation that the actual motion matched the
    intent. Equals the ``Correction.dx_px`` we received."""

    correction_dy_px: float


# ---------------------------------------------------------------------------
# PipelineState
# ---------------------------------------------------------------------------


@dataclass
class PipelineState:
    """Authoritative per-pipeline state.

    Operator-controlled fields are set via Controller commands.
    Auto-controlled fields are set by Solver via Controller (so the
    Controller arbitrates).
    Observed fields are written by Solver as it produces results.
    """

    # Identity
    pipeline_id: str
    camera_id: str

    # --- Operator-controlled ---
    mode: Mode = Mode.OFF
    selection_policy: str = "brightest_in_window"
    method: str = "dummy"
    method_params: dict[str, Any] = field(default_factory=dict)
    exp_time: float = 1.0
    binning: int | tuple[int, int] = 1
    gain: int | None = None
    frequency: float = 1.0
    central_point: tuple[float, float] = (1024.0, 1024.0)
    # Default reticle position from camera config — UI uses for "home"
    # to restore the operator's reticle after dragging. Immutable
    # post-startup. None when unset (UI falls back to camera centre).
    central_point_default: tuple[float, float] | None = None
    # Persisted across lock-loss: the position and ADU of the most
    # recent SUCCESSFUL detection. Used by wide-search to favour the
    # same physical star (proximity + ADU similarity) when re-acquiring
    # after a brief lock drop. Distinct from ``acquired_pos`` (which is
    # cleared on loss). Reset on operator-forced restart
    # (``acquire``, ``lock_at`` to a new spot, mode→OFF).
    last_acquired_pos: tuple[float, float] | None = None
    last_acquired_adu: float | None = None
    wide_search_radius_px: int = 200
    search_reg_px: int = 25
    stacking_count: int = 1
    stacking_method: str = "median"
    corrections_avg_no: int = 5
    corrections_avg_method: str = "median"
    adu_match_tolerance_per_sec: float | None = 5_000.0
    auto_exposure: AutoExposureConfig = field(default_factory=AutoExposureConfig)
    roi: ROIConfig = field(default_factory=ROIConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    preprocessing: PreprocessingConfig = field(default_factory=PreprocessingConfig)
    hole_detect: HoleDetectConfig = field(default_factory=HoleDetectConfig)
    save_raw_fits: bool = False
    save_stacked_fits: bool = False
    save_raw_thumbnails: bool = False
    save_stacked_thumbnails: bool = False

    # --- Auto-controlled (set by Solver via Controller) ---
    acquired: bool = False
    acquired_pos: tuple[float, float] | None = None
    acquired_adu: float | None = None
    acquired_at_ts: list[int] | None = None  # serverish timestamp array
    current_exp_time: float | None = None
    """Auto-exposure override. When None, the camera uses ``exp_time``
    (the operator-set baseline). Auto-exposure writes this to override
    transiently; operator ``set_state(exp_time=…)`` clears it back to
    None so the operator value wins until auto-exposure decides again."""
    current_roi: tuple[int, int, int, int] | None = None
    frame_phase: str | None = None
    """Phase of the most recently processed frame
    (``FramePhase.value``). Updated by Solver on every iteration so
    the UI can render mode-appropriate overlays (skip-detect arrow
    during IN_FLIGHT/SETTLING, predicted-position search circle during
    ACQUIRING). ``None`` means no frame has been processed yet (cold
    start) or no acquisition history is being maintained.
    """

    active_pulse: PulseEvent | None = None
    """First-class temporal record of an in-flight pulse, written by
    Enforcer at issue time, cleared by Controller on the first
    post-settle successful acquire. Source of truth for the
    TRACKING/IN_FLIGHT/SETTLING/ACQUIRING phase the pipeline is in;
    Solver classifies each frame against ``active_pulse.{issued,
    motion_end, settled}_utc`` and chooses behaviour accordingly.

    ``None`` ⇔ no pulse in flight ⇔ pipeline is in TRACKING phase.

    See ``PulseEvent`` docstring for full lifecycle. ``predicted_pos``
    below mirrors ``active_pulse.predicted_pos`` for now (legacy
    consumers still read it); a follow-up commit consolidates."""

    predicted_pos: tuple[float, float] | None = None
    """Where the star is expected to be on the next clean frame, given
    the most recently issued pulse. Written by Enforcer immediately after
    each pulse using the forward Jacobian (motion = J · pulse, with the
    actually-issued damped+clipped pulse durations). Cleared by the
    Controller on a fresh successful acquire (the prediction has done
    its job — narrow search latched onto the star at the expected spot).

    Why this exists: drop-to-reticle and other multi-frame slews move
    the star a long way over several pulse cycles. Centring the narrow
    search box on the *previous* ``acquired_pos`` is the wrong question
    — by the time the next frame arrives, the star is closer to the
    target than to where it was last seen. With ``predicted_pos`` the
    box tracks the pulse-induced motion, the star stays inside, lock
    survives the slew, and the operator gets reliable drop-to-reticle.
    """
    guide_anchor: tuple[float, float] | None = None
    """The pixel position guider corrects toward during ``guiding`` mode.
    Captured at the moment guiding starts (``acquired_pos`` snapshot)
    so the operator's natural mental model "hold star where I locked it"
    works without thinking. Distinct from ``central_point`` — that one
    is the *target reticle* the operator may drag (right-click) and
    eventually drives the pulse-slew "drag-star-to-fiber" workflow.
    None when not guiding; cleared on every mode transition out of
    guiding so the next guiding session re-snapshots fresh."""

    # --- Observed (written by Solver) ---
    last_correction_dx_px: float | None = None
    last_correction_dy_px: float | None = None
    last_correction_drot_rad: float | None = None
    fwhm_recent: float | None = None
    rotation_recent: float | None = None
    hole_candidate: dict[str, Any] | None = None
    """Latest tracked reticle-target (fibre-entrance) measurement, or
    None when the detector has nothing to report. Shape:
    ``{x, y, offset_px, snr, scatter_px, samples, refinable, reason}``
    (see ``hole_detect.HoleCandidate``).

    Written by Solver via Controller, independently of solver method and
    mode, so a refinement can be prepared while still in monitoring.
    ``refinable`` is the authoritative gate for offering the
    ``refine_reticle`` action; ``reason`` is operator-facing text
    explaining the current state either way."""

    candidates: list[tuple[float, float, float]] | None = None
    """Per-frame detection list: ``[(x, y, adu), …]`` in rank order
    (best-first per the active solver's ``rank_by`` policy). Surfaced
    so operators can see *every* peak the detector found — useful for
    verifying that hot pixels are/aren't surviving the masks, and for
    the UI's TAB-cycle "lock the next candidate" interaction. ``None``
    when no detection has run yet (or the solver doesn't produce a
    candidate list)."""

    # --- Meta ---
    version: int = 0
    """Bumped on every mutation by Controller. Stages compare to detect
    state changes between iterations."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (StrEnum members serialise as values)."""
        d = asdict(self)
        d["mode"] = self.mode.value
        return d


# ---------------------------------------------------------------------------
# Lock + snapshot wrapper
# ---------------------------------------------------------------------------


class PipelineStateHolder:
    """Wraps PipelineState with an asyncio.Lock for write serialisation
    and a lock-free `snapshot()` for stage reads.

    Stages call `snapshot()` at the start of each iteration; if `version`
    changed since the last iteration they reconfigure.

    Only Controller should call `update()`. Direct external mutation is
    not prevented (Python doesn't have good private state) but is a
    design violation.
    """

    def __init__(self, initial: PipelineState) -> None:
        self._state = initial
        self._lock = asyncio.Lock()

    async def update(self, **changes: Any) -> int:
        """Atomic partial update; returns new version."""
        async with self._lock:
            for key, value in changes.items():
                if not hasattr(self._state, key):
                    raise AttributeError(f"PipelineState has no field {key!r}")
                setattr(self._state, key, value)
            self._state.version += 1
            return self._state.version

    def snapshot(self) -> PipelineState:
        """Return a deep copy of the current state (lock-free read).

        Deep copy under the GIL is atomic enough for our purposes —
        worst case a stage sees a snapshot from one version-step ago,
        which is harmless: it'll see the new state on the next iteration.
        """
        return copy.deepcopy(self._state)

    @property
    def version(self) -> int:
        return self._state.version

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock
