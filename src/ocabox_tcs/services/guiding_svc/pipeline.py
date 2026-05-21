"""Pipeline — one stage chain bound to one PipelineState.

A Pipeline owns:
  - A `PipelineStateHolder` (state)
  - Stage instances (Stacker, Solver, Enforcer)
  - Bounded asyncio.Queues between stages
  - A subscription to a CameraArrayCollector

Lifecycle: `start()` opens stages and begins consuming frames;
`stop()` shuts everything down cleanly.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ocabox_tcs.services.guiding_svc.camera_array_collector import (
    CameraArrayCollector,
    ExposureJob,
)
from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame, RawFrame
from ocabox_tcs.services.guiding_svc.stages.enforcer import Enforcer
from ocabox_tcs.services.guiding_svc.stages.solver import Solver, SolverMethod
from ocabox_tcs.services.guiding_svc.stages.stacker import Stacker
from ocabox_tcs.services.guiding_svc.state import Mode, PipelineState, PipelineStateHolder


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class Pipeline:
    """One processing pipeline tied to a single PipelineState.

    Args:
        initial_state: PipelineState to bootstrap from (typically built
            from YAML config).
        collector: The CameraArrayCollector for this pipeline's camera
            (shared with sibling pipelines).
        method: SolverMethod instance to use.
        queue_depth: Per-queue bound (backpressure point).
        mount: Optional mount handle for Enforcer.
        pulse_guide_model: Optional ``AdaptiveTransform`` for the Enforcer.
        enforcer_kwargs: Extra kwargs forwarded to ``Enforcer.__init__``
            (``damping``, ``saturation_ms``, ``min_pulse_ms``).
    """

    def __init__(
        self,
        initial_state: PipelineState,
        collector: CameraArrayCollector,
        method: SolverMethod,
        queue_depth: int = 4,
        mount: Any | None = None,
        *,
        pulse_guide_model: Any | None = None,
        enforcer_kwargs: dict[str, Any] | None = None,
        thumbnail_emitter: Any | None = None,
    ) -> None:
        self.state = PipelineStateHolder(initial_state)
        self.collector = collector
        self.method = method
        self.queue_depth = queue_depth
        self.mount = mount

        # Inter-stage queues
        self._raw_q: asyncio.Queue[RawFrame] = asyncio.Queue(maxsize=queue_depth)
        self._analysis_q: asyncio.Queue[AnalysisFrame] = asyncio.Queue(maxsize=queue_depth)
        self._correction_q: asyncio.Queue[Correction] = asyncio.Queue(maxsize=queue_depth)

        # Optional analysis-frame tap for the ThumbnailEmitter. Stacker
        # publishes to it non-blocking (drop-oldest) so a slow consumer
        # never throttles Solver.
        self._thumbnail_emitter = thumbnail_emitter
        extra_outs = []
        if thumbnail_emitter is not None:
            extra_outs.append(thumbnail_emitter.in_queue)

        self._stacker = Stacker(
            self._raw_q, self._analysis_q, self.state,
            extra_out_queues=extra_outs,
        )
        self._solver = Solver(self._analysis_q, self._correction_q, self.state, method)
        self._enforcer = Enforcer(
            self._correction_q,
            self.state,
            mount=mount,
            pulse_guide_model=pulse_guide_model,
            **(enforcer_kwargs or {}),
        )

        self._subscription = None
        self._started = False
        # Lightweight runtime counters reported via the manager's
        # metrics callback. The cycle ratio (acquired / processed) is
        # the cheapest health signal — a healthy guider in monitoring
        # or guiding mode keeps it close to 1.0; values dropping toward
        # zero signal a problem (camera shared with another observer,
        # bad seeing, lost star). Wall-clock timestamps let the
        # monitor's healthcheck flag "no progress in N seconds" as
        # DEGRADED.
        self._cycles_total = 0       # solver invocations since start
        self._cycles_acquired = 0    # solver invocations that resulted in a lock
        self._last_cycle_ts: float = 0.0     # monotonic; 0 = never
        self._last_lock_ts: float = 0.0      # monotonic; 0 = never

    @property
    def pipeline_id(self) -> str:
        return self.state.snapshot().pipeline_id

    def swap_method(self, method_name: str, method_params: dict[str, Any]) -> None:
        """Replace the active solver method at runtime.

        Used when the operator toggles between methods in the UI (e.g.
        ``single_star`` ↔ ``fiber_photocentroid``). The Solver task keeps
        running on the same queues; only the per-frame algorithm changes.

        Lookups + instantiation use the global ``METHODS`` registry. The
        new method receives the same controller hook as the old one, so
        ``notify_acquired`` continues to flow.

        Side effects:
        - Solver task continues processing whatever frame it's already
          mid-flight on with the OLD method (~one frame of lag).
        - Method-internal state is gone (no transfer); the new method
          treats the very next frame as a cold start. Acceptable —
          fiber/single share PipelineState (acquired/anchor/etc.) and
          method-internal state was tiny (narrow-miss counter etc.).
        """
        from ocabox_tcs.services.guiding_svc.stages.solver.methods import METHODS  # noqa: PLC0415
        cls = METHODS.get(method_name)
        if cls is None:
            raise ValueError(f"Unknown solver method {method_name!r}")
        new_method = cls(**(method_params or {}))
        self.method = new_method
        if hasattr(self._solver, "method"):
            self._solver.method = new_method
        if hasattr(self._solver, "_controller") and hasattr(new_method, "controller"):
            new_method.controller = self._solver._controller
        logger.info(
            "Pipeline %s method swapped → %s (params keys: %s)",
            self.pipeline_id, method_name, list((method_params or {}).keys()),
        )

    async def start(self) -> None:
        if self._started:
            return
        snapshot = self.state.snapshot()
        # Always subscribe — pause/resume on mode transitions instead
        # of subscribe/unsubscribe churn. The collector's drive loop
        # parks itself on an idle event when *all* its subscribers are
        # paused, so OFF mode → zero camera I/O without dropping the
        # subscription registration.
        self._subscription = self.collector.subscribe_stream(
            pipeline_id=snapshot.pipeline_id,
            out_queue=self._raw_q,
            get_params=self._build_exposure_job,
        )
        if snapshot.mode == Mode.OFF:
            self._subscription.set_active(False)
            logger.info(
                "Pipeline %s starts paused (mode=OFF — camera idle)",
                snapshot.pipeline_id,
            )
        await self._stacker.start()
        if self._thumbnail_emitter is not None:
            await self._thumbnail_emitter.start()
        await self._solver.start()
        await self._enforcer.start()
        self._started = True
        logger.info("Pipeline %s started (mode=%s, method=%s)",
                    snapshot.pipeline_id, snapshot.mode.value, self.method.name)

    async def stop(self) -> None:
        if not self._started:
            return
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None
        await self._enforcer.stop()
        await self._solver.stop()
        if self._thumbnail_emitter is not None:
            await self._thumbnail_emitter.stop()
        await self._stacker.stop()
        self._started = False
        logger.info("Pipeline %s stopped", self.pipeline_id)

    def record_cycle(self, *, acquired: bool) -> None:
        """Solver hook — called once per detection cycle. Bumps the
        pipeline's runtime counters used by the manager's metric
        callback. ``acquired=True`` means the cycle ended with the
        solver holding (or refreshing) a lock — successful work."""
        import time as _time
        now = _time.monotonic()
        self._cycles_total += 1
        self._last_cycle_ts = now
        if acquired:
            self._cycles_acquired += 1
            self._last_lock_ts = now

    def runtime_snapshot(self) -> dict[str, Any]:
        """Compact runtime stats for the metrics callback. Wall-clock
        seconds-since for last-cycle / last-lock so consumers don't
        need to mirror our monotonic clock."""
        import time as _time
        now = _time.monotonic()
        ratio = (
            self._cycles_acquired / self._cycles_total
            if self._cycles_total > 0 else None
        )
        return {
            "cycles_total": self._cycles_total,
            "cycles_acquired": self._cycles_acquired,
            "cycle_acquired_ratio": ratio,
            "last_cycle_age_s": (
                round(now - self._last_cycle_ts, 2) if self._last_cycle_ts > 0 else None
            ),
            "last_lock_age_s": (
                round(now - self._last_lock_ts, 2) if self._last_lock_ts > 0 else None
            ),
        }

    def apply_mode(self, mode: Mode) -> None:
        """Resume / pause camera fetching to match the requested mode.

        OFF → pause the collector subscription so the camera idles and
        any second observer sharing the device gets relief. Any
        non-OFF mode → resume (idempotent if already active). The
        subscription stays registered across the toggle so we don't
        churn collector state on mode flips.

        On resume we wipe ``_last_cycle_ts`` so the healthcheck doesn't
        see a stale "last frame was 7 minutes ago" timestamp from the
        previous active session and immediately flag DEGRADED. The
        next solver iteration repopulates it; until then
        ``runtime_snapshot.last_cycle_age_s`` returns ``None`` and the
        manager treats that as "no data yet, not a stall".
        """
        if self._subscription is None:
            return
        was_active = self._subscription.active
        want_active = mode != Mode.OFF
        self._subscription.set_active(want_active)
        if want_active and not was_active:
            self._last_cycle_ts = 0.0
        logger.info(
            "Pipeline %s subscription %s (mode=%s)",
            self.pipeline_id,
            "active" if want_active else "paused",
            mode.value,
        )

    def _build_exposure_job(self) -> ExposureJob:
        snapshot = self.state.snapshot()
        # current_exp_time is the auto-exposure override; None means
        # "no override active, use the operator-set baseline".
        exp_time = (
            snapshot.current_exp_time
            if snapshot.current_exp_time is not None
            else snapshot.exp_time
        )
        return ExposureJob(
            pipeline_id=snapshot.pipeline_id,
            exp_time=exp_time,
            roi=snapshot.current_roi,
            binning=snapshot.binning,
            gain=snapshot.gain,
        )
