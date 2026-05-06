"""Solver base — orchestrates per-frame analysis using a pluggable
SolverMethod and SelectionPolicy.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame
from ocabox_tcs.services.guiding_svc.state import PipelineStateHolder


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class SolverMethod(Protocol):
    """Interface for a Solver method (single_star, multi_star, …).

    Methods are stateful: they retain `acquired_pos`, reference frames,
    rolling history, etc. across frames.
    """

    name: str
    """Method identifier (matches PipelineState.method)."""

    uses_adu_match: bool
    """Whether this method uses ADU-tolerance matching for re-acquisition."""

    produces_rotation: bool
    """Whether this method estimates field rotation in addition to
    translation."""

    async def solve(
        self,
        frame: AnalysisFrame,
        state: dict[str, Any],
    ) -> Correction | None:
        """Compute a correction from a single AnalysisFrame.

        `state` is a snapshot dict from PipelineStateHolder.snapshot().
        Returns None if the method couldn't produce a correction
        (star_lost, low_confidence, etc.) — caller publishes a
        `star_lost` event.
        """
        ...

    def reset(self) -> None:
        """Discard accumulated state (acquired_pos, ref image, history)."""
        ...


class Solver:
    """Solver stage: consumes AnalysisFrame, produces Correction.

    Holds:
        - a `SolverMethod` instance (chosen per PipelineState.method)
        - a rolling window of recent corrections for averaging
        - hooks for selection_policy + reference-frame freshness

    Args:
        in_queue: Bounded asyncio.Queue of AnalysisFrame (from Stacker).
        out_queue: Bounded asyncio.Queue of Correction (to Controller / Enforcer).
        state: Shared pipeline state holder.
        method: A SolverMethod implementation.
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[AnalysisFrame],
        out_queue: asyncio.Queue[Correction],
        state: PipelineStateHolder,
        method: SolverMethod,
    ) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.state = state
        self.method = method
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._recent_corrections: list[Correction] = []
        # Optional Controller reference, wired post-construction by the
        # Manager (see manager.py). Methods that need to mutate state
        # (e.g. SingleStarMethod toggling `acquired`) call through it;
        # when unwired (sim/dev path) the method falls back to no-op.
        self._controller: Any | None = None

    def set_controller(self, controller: Any) -> None:
        """Wire a Controller into the Solver.

        The Solver mirrors the reference onto the active method (when the
        method exposes a `controller` attribute) so methods can publish
        lock-state changes via `controller.notify_acquired(...)`.
        """
        self._controller = controller
        if hasattr(self.method, "controller"):
            self.method.controller = controller

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="solver")
        logger.debug("Solver started (method=%s)", self.method.name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                frame = await self.in_queue.get()
            except asyncio.CancelledError:
                break

            snapshot = self.state.snapshot().to_dict()
            try:
                correction = await self.method.solve(frame, snapshot)
            except Exception as e:  # noqa: BLE001
                logger.exception("Solver method failed: %s", e)
                continue

            if correction is not None:
                self._recent_corrections.append(correction)
                # Trim to corrections_avg_no
                avg_no = snapshot.get("corrections_avg_no", 5)
                if len(self._recent_corrections) > avg_no:
                    self._recent_corrections = self._recent_corrections[-avg_no:]
                await self.out_queue.put(correction)

    def averaged_correction(self) -> Correction | None:
        """Compute averaged correction over the rolling window.

        Used by Enforcer when authorised. Stub-only for the frame
        iteration; Enforcer logs raw corrections.
        """
        raise NotImplementedError(
            "Solver.averaged_correction — apply corrections_avg_method "
            "(median/mean/weighted) over self._recent_corrections. Trivial "
            "to implement; left as stub for frame iteration to keep "
            "Enforcer behaviour explicit."
        )
