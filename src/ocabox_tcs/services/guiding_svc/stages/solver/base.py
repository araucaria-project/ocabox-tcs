"""Solver base — orchestrates per-frame analysis using a pluggable
SolverMethod and SelectionPolicy.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.hole_detect import HoleDetectConfig, HoleTracker
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
        # Reticle-target (fibre-entrance) detector. Deliberately owned by
        # the Solver rather than by a method: the operator needs it to
        # work while ``single_star`` is active in monitoring, so that a
        # reticle refinement can be prepared before switching to fibre
        # guiding. See hole_detect.py for the rationale.
        self._hole_tracker = HoleTracker()
        self._hole_frames = 0
        self._hole_last_refinable: bool | None = None
        self._hole_center_ref: tuple[float, float] | None = None
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

            # Bound detection work. A pathological frame (extreme
            # noise, partially-saturated everything, FFS internal
            # spin) must not freeze the whole pipeline — better to
            # drop the frame and try the next one. 5× exp_time is a
            # generous ceiling; real detection runs in tens of ms.
            exp_time = float(snapshot.get("exp_time", 1.0)) or 1.0
            try:
                correction = await asyncio.wait_for(
                    self.method.solve(frame, snapshot),
                    timeout=max(5.0 * exp_time, 5.0),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Solver method timed out after %.1fs — dropping frame",
                    max(5.0 * exp_time, 5.0),
                )
                continue
            except Exception as e:  # noqa: BLE001
                logger.exception("Solver method failed: %s", e)
                continue

            # Reticle-target detection runs on every frame, whatever the
            # method decided. Never allowed to break guiding: a detector
            # failure costs a candidate, not a correction.
            try:
                await self._update_hole_candidate(frame, snapshot)
            except Exception as e:  # noqa: BLE001
                logger.exception("hole detection failed: %s", e)

            if correction is not None:
                self._recent_corrections.append(correction)
                # Trim to corrections_avg_no
                avg_no = snapshot.get("corrections_avg_no", 5)
                if len(self._recent_corrections) > avg_no:
                    self._recent_corrections = self._recent_corrections[-avg_no:]
                # Drop-oldest on the correction queue. Real-time
                # guiding: a stale correction from N frames ago is
                # worse than no correction (mount has moved since;
                # the latest frame's correction supersedes). Producer
                # must never block on a slow Enforcer — if the queue
                # fills, the freshest correction wins.
                try:
                    self.out_queue.put_nowait(correction)
                except asyncio.QueueFull:
                    try:
                        self.out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.out_queue.put_nowait(correction)
                    except asyncio.QueueFull:
                        logger.warning(
                            "correction queue full after drain; dropping",
                        )

    #: Publish cadence for the tracked candidate (frames). The tracked
    #: value changes slowly (a static feature, median-filtered), so
    #: pushing it on every frame would only add state churn; a flip of
    #: ``refinable`` is published immediately regardless.
    _HOLE_PUBLISH_EVERY_N = 4

    async def _update_hole_candidate(
        self, frame: AnalysisFrame, snapshot: dict[str, Any]
    ) -> None:
        """Run the reticle-target detector and forward the result.

        Skips frames taken while the mount is moving or settling — the
        image is smeared and the measurement would pollute the tracker's
        consistency test with real motion.
        """
        cfg_raw = snapshot.get("hole_detect") or {}
        cfg = HoleDetectConfig(**cfg_raw) if isinstance(cfg_raw, dict) else cfg_raw
        if not cfg.enabled or self._controller is None:
            return
        central = snapshot.get("central_point")
        if central is None:
            return
        central = (float(central[0]), float(central[1]))

        # A moved aim point invalidates accumulated evidence: samples
        # were judged against the previous centre.
        if self._hole_center_ref != central:
            self._hole_tracker.reset()
            self._hole_center_ref = central
            self._hole_last_refinable = None

        phase = snapshot.get("frame_phase")
        if phase in ("in_flight", "settling"):
            return

        candidate = self._hole_tracker.update(frame.array, central, cfg)
        self._hole_frames += 1
        refinable = bool(candidate.refinable) if candidate is not None else False
        due = (self._hole_frames % self._HOLE_PUBLISH_EVERY_N) == 0
        flipped = refinable != self._hole_last_refinable
        if not (due or flipped):
            return
        self._hole_last_refinable = refinable
        await self._controller.notify_hole_candidate(
            candidate.to_dict() if candidate is not None else None
        )

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
