"""Stacker stage — frame timing, stacking, calibration preprocessing.

Skeleton: lifecycle structure present; calibration and stacking math are
stubbed pending the pyaraucaria.images_stacking extraction.
"""

from __future__ import annotations

import asyncio
import logging

from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame, RawFrame
from ocabox_tcs.services.guiding_svc.state import PipelineStateHolder


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class Stacker:
    """Consumes RawFrame, produces AnalysisFrame.

    Internal pipeline:
        RawFrame
        ├─ frequency align (drop or wait per pipeline frequency)
        ├─ accumulate stacking_count frames
        ├─ stack (median/mean/sum, optional sigma_clip)
        ├─ apply preprocessing chain (bias / dark / flat / bad-pixel /
        │                              saturation)
        └─ AnalysisFrame

    Args:
        in_queue: Bounded asyncio.Queue of RawFrame (from CameraArrayCollector).
        out_queue: Bounded asyncio.Queue of AnalysisFrame (to Solver).
        state: Shared pipeline state holder (read-only).
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[RawFrame],
        out_queue: asyncio.Queue[AnalysisFrame],
        state: PipelineStateHolder,
        extra_out_queues: list[asyncio.Queue[AnalysisFrame]] | None = None,
    ) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.state = state
        self.extra_out_queues = list(extra_out_queues or [])
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="stacker")
        logger.debug("Stacker started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.debug("Stacker stopped")

    async def _run(self) -> None:
        """Main loop. Frame-iteration version: pass-through (1:1 raw→analysis,
        no stacking, no calibration) so end-to-end smoke runs.
        Real stacking + calibration will land
        """
        while self._running:
            try:
                raw = await self.in_queue.get()
            except asyncio.CancelledError:
                break

            # Frame-iteration pass-through: wrap RawFrame as AnalysisFrame
            # with no stacking / no calibration. This lets dummy methods
            # produce something visible end-to-end.
            analysis = AnalysisFrame(
                array=raw.array,
                exp_time_total=raw.exp_time,
                n_stacked=1,
                timestamp=raw.timestamp,
                roi=raw.roi,
                metadata={**raw.metadata, "stacker": "passthrough_frame_iter"},
            )
            await self.out_queue.put(analysis)
            for q in self.extra_out_queues:
                # Non-blocking tap: drop the oldest element on full so a slow
                # consumer (e.g. ThumbnailEmitter on contended NFS) never
                # backpressures the primary Solver path.
                while q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(analysis)
                except asyncio.QueueFull:
                    pass

    # -- placeholders for later iterations ------------------------------

    def _stack(self, frames: list[RawFrame]) -> AnalysisFrame:
        raise NotImplementedError(
            "Stacker._stack — implement using pyaraucaria.images_stacking "
            "(post-extraction). Honour "
            "stacking_method (median/mean/sum) and optional sigma_clip "
            "from PipelineState."
        )

    def _apply_calibration(self, frame: RawFrame) -> RawFrame:
        raise NotImplementedError(
            "Stacker._apply_calibration — bias + dark_current scalable "
            "strategy Honour PipelineState."
            "calibration. Skip if calibration.bias.enabled=False."
        )

    def _apply_preprocessing(self, frame: RawFrame) -> RawFrame:
        raise NotImplementedError(
            "Stacker._apply_preprocessing — bad pixel mask + saturation "
            "masking Cheap operations; "
            "saturation always-on, bad-pixel mask conditional."
        )
