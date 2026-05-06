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

        self._stacker = Stacker(self._raw_q, self._analysis_q, self.state)
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

    @property
    def pipeline_id(self) -> str:
        return self.state.snapshot().pipeline_id

    async def start(self) -> None:
        if self._started:
            return
        snapshot = self.state.snapshot()
        if snapshot.mode == Mode.OFF:
            logger.info(
                "Pipeline %s in OFF mode — not subscribing to collector",
                snapshot.pipeline_id,
            )
        else:
            self._subscription = self.collector.subscribe_stream(
                pipeline_id=snapshot.pipeline_id,
                out_queue=self._raw_q,
                get_params=self._build_exposure_job,
            )
        await self._stacker.start()
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
        await self._stacker.stop()
        self._started = False
        logger.info("Pipeline %s stopped", self.pipeline_id)

    def _build_exposure_job(self) -> ExposureJob:
        snapshot = self.state.snapshot()
        return ExposureJob(
            pipeline_id=snapshot.pipeline_id,
            exp_time=snapshot.current_exp_time or snapshot.exp_time,
            roi=snapshot.current_roi,
            binning=snapshot.binning,
            gain=snapshot.gain,
        )
