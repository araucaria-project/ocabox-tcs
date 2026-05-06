"""CameraArrayCollector — single hardware owner per camera.


For frame iteration: a thin wrapper around the Backend, producing
RawFrame to a single bound queue. Priority queue / aging / coalescence
are deferred until a second pipeline-on-camera scenario lands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ocabox_tcs.services.guiding_svc.backends.base import CollectorBackend
from ocabox_tcs.services.guiding_svc.stages.base import RawFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


@dataclass
class ExposureJob:
    """Atomic exposure parameter set submitted to the camera operator.

    Frame iteration: only `exp_time` and `pipeline_id` are honoured;
    priority/aging/ROI/binning slots present for future implementation.
    """

    pipeline_id: str
    exp_time: float
    roi: tuple[int, int, int, int] | None = None
    binning: int | tuple[int, int] = 1
    gain: int | None = None
    priority: int = 0
    age_factor: float = 1.0
    submitted_at_monotonic: float = 0.0
    deadline: float | None = None

    def effective_priority(self, now: float) -> float:
        return self.priority + (now - self.submitted_at_monotonic) * self.age_factor


class CameraArrayCollector:
    """Owns one camera; serves ExposureJob requests.

    **Frame iteration**: drives a single subscriber loop reading from one
    output queue. No real priority queue, no aging. Just enough to wire
    end-to-end smoke.

    **Future iterations**:
      - Multi-pipeline subscriptions
      - Priority queue with aging
      - Compatible-job coalescence

    Args:
        camera_id: Identifier for logs / events / state.
        backend: One of {SimBackend, DirectFetchBackend, DownloaderRPCBackend, FitsWatchBackend}.
    """

    def __init__(self, camera_id: str, backend: CollectorBackend) -> None:
        self.camera_id = camera_id
        self.backend = backend
        self._open = False
        self._streams: list[_StreamSubscription] = []
        self._driver_task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if self._open:
            return
        await self.backend.open()
        self._open = True
        logger.info("CameraArrayCollector(%s) open via %s", self.camera_id, self.backend.name)

    async def close(self) -> None:
        if self._driver_task:
            self._driver_task.cancel()
            try:
                await self._driver_task
            except asyncio.CancelledError:
                pass
            self._driver_task = None
        if self._open:
            await self.backend.close()
            self._open = False

    # -- API consumed by Pipeline ---------------------------------------

    async def submit_one(self, job: ExposureJob) -> RawFrame:
        """Single-shot fetch (snapshot, calib build, etc.)."""
        if not self._open:
            raise RuntimeError(f"CameraArrayCollector({self.camera_id}) not open")
        fetched = await self.backend.submit_one(
            exp_time=job.exp_time,
            roi=job.roi,
            binning=job.binning,
            gain=job.gain,
        )
        return RawFrame(
            array=fetched.array,
            exp_time=fetched.exp_time,
            timestamp=fetched.timestamp,
            roi=fetched.roi,
            binning=fetched.binning,
            gain=fetched.gain,
            metadata=fetched.metadata,
        )

    def subscribe_stream(
        self,
        pipeline_id: str,
        out_queue: asyncio.Queue[RawFrame],
        get_params,
    ) -> _StreamSubscription:
        """Register a continuous-fetch subscription.

        `get_params()` is a callable returning the current exposure params
        for the requesting pipeline. The collector calls it at each cycle
        to pick up auto-exposure / ROI changes.

        Frame iteration: simple round-robin loop dispatching to all active
        subscribers in order. No priority arbitration.
        """
        sub = _StreamSubscription(pipeline_id=pipeline_id, out_queue=out_queue, get_params=get_params)
        self._streams.append(sub)
        if self._driver_task is None and self._open:
            self._driver_task = asyncio.create_task(self._drive(), name=f"cam-{self.camera_id}")
        return sub

    async def _drive(self) -> None:
        """Round-robin fetch loop. Frame iteration only — replace with
        priority-queue scheduler when multi-pipeline scenarios land."""
        while True:
            for sub in list(self._streams):
                if not sub.active:
                    continue
                try:
                    params = sub.get_params()
                except Exception as e:  # noqa: BLE001
                    logger.exception("subscriber %s get_params failed: %s", sub.pipeline_id, e)
                    continue
                try:
                    raw = await self.submit_one(params)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.exception("backend submit_one failed: %s", e)
                    await asyncio.sleep(1.0)
                    continue
                try:
                    sub.out_queue.put_nowait(raw)
                except asyncio.QueueFull:
                    # Drop frame — downstream stage is slow, keep going.
                    logger.warning("subscriber %s queue full, dropping frame", sub.pipeline_id)
            if not self._streams:
                # No subscribers — exit cleanly.
                self._driver_task = None
                return
            # Yield to event loop so other tasks can run.
            await asyncio.sleep(0)


@dataclass
class _StreamSubscription:
    pipeline_id: str
    out_queue: asyncio.Queue[RawFrame]
    get_params: object  # Callable[[], ExposureJob]
    active: bool = True

    def cancel(self) -> None:
        self.active = False
