"""CameraArrayCollector — single hardware owner per camera.


For frame iteration: a thin wrapper around the Backend, producing
RawFrame to a single bound queue. Priority queue / aging / coalescence
are deferred until a second pipeline-on-camera scenario lands.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

import numpy as np

from ocabox_tcs.services.guiding_svc.backends.base import CollectorBackend
from ocabox_tcs.services.guiding_svc.stages.base import RawFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class FrameDeduplicator:
    """Detects a frozen camera buffer by frame-content digest.

    A real sensor never produces two byte-identical frames — photon and
    read noise differ everywhere — so *consecutive identical content* has
    exactly one meaning: the acquisition chain is re-serving a stale
    buffer (driver contention with a second Alpaca client, firmware
    hiccup, transport replay). Feeding such frames downstream is
    dangerous: the solver detects the frozen star at a frozen offset and
    the enforcer "corrects" it forever — on 2026-07-29 this walked the
    jk15 mount blindly for 10.5 h (~7 000 identical pulses). See
    ``doc/guider/NIGHT_REPORT_2026-07-29_stefan.md`` §2.3.

    Digest covers a strided subsample (fast, ~30 k px on a 2 MPix frame)
    plus shape/dtype. Subsampling is safe for the "identical buffer"
    question: any genuinely new exposure differs in essentially every
    pixel, so collisions on the subsample require the full buffer to be
    identical too — which is the very condition we're detecting.

    Args:
        stride: Subsample step in both axes for the digest.
        alert_after: Consecutive duplicates that escalate the
            once-per-run warning to an error (camera declared frozen).
    """

    def __init__(self, *, stride: int = 8, alert_after: int = 10) -> None:
        self.stride = int(stride)
        self.alert_after = int(alert_after)
        self._last_digest: bytes | None = None
        self.duplicate_run = 0
        """Length of the current consecutive-duplicate run (0 = healthy)."""
        self.duplicates_total = 0
        """All duplicate frames seen since startup (diagnostics)."""

    def is_duplicate(self, array: np.ndarray) -> bool:
        """Digest ``array`` and report whether it repeats the previous frame.

        Updates run/total counters and emits rate-limited logs: WARNING
        on the first duplicate of a run, ERROR once when the run reaches
        ``alert_after``, INFO on recovery (fresh frame after a run).
        """
        h = hashlib.blake2b(digest_size=16)
        h.update(str(array.shape).encode())
        h.update(str(array.dtype).encode())
        sub = array[:: self.stride, :: self.stride] if array.ndim == 2 else array
        h.update(np.ascontiguousarray(sub).tobytes())
        digest = h.digest()

        if digest == self._last_digest:
            self.duplicate_run += 1
            self.duplicates_total += 1
            if self.duplicate_run == 1:
                logger.warning(
                    "Duplicate frame content detected — camera served a "
                    "stale buffer (driver contention / firmware quirk). "
                    "Dropping frame; solver sees nothing this cycle."
                )
            elif self.duplicate_run == self.alert_after:
                logger.error(
                    "Camera appears FROZEN: %d consecutive identical "
                    "frames. No corrections are being produced; guiding "
                    "is effectively suspended until fresh frames arrive "
                    "(check for a second Alpaca client, restart the "
                    "camera if it persists).",
                    self.duplicate_run,
                )
            return True

        if self.duplicate_run > 0:
            logger.info(
                "Fresh frame after %d duplicate(s) — camera recovered.",
                self.duplicate_run,
            )
        self.duplicate_run = 0
        self._last_digest = digest
        return False


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
        # Frozen-buffer guard on the streaming path. One-shot fetches
        # (``submit_one`` callers: calibration builds, snapshots) bypass
        # it — they have no "previous frame" semantics.
        self.dedup = FrameDeduplicator()
        # Wake signal for the drive loop. Set when at least one
        # subscriber is active (or one resumes); cleared by the loop
        # when it observes "all paused" so it can park until something
        # changes.
        self._idle_event = asyncio.Event()
        self._idle_event.set()

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
        sub = _StreamSubscription(
            pipeline_id=pipeline_id, out_queue=out_queue, get_params=get_params,
            _collector=self,
        )
        self._streams.append(sub)
        # New subscriber implies "something to do" — wake any parked
        # drive loop. Idempotent: ``set()`` on an already-set event
        # is a no-op.
        self._idle_event.set()
        if self._driver_task is None and self._open:
            self._driver_task = asyncio.create_task(self._drive(), name=f"cam-{self.camera_id}")
        return sub

    async def _drive(self) -> None:
        """Round-robin fetch loop. Frame iteration only — replace with
        priority-queue scheduler when multi-pipeline scenarios land.

        Idle behaviour: when no subscriber is active in a cycle (all
        paused — typically every pipeline is in OFF mode) we don't
        hammer the camera. The loop sleeps on ``_idle_event`` until a
        subscriber resumes via ``set_active(True)`` or a new one
        registers. Camera I/O drops to zero in OFF mode without tearing
        the subscription down. ``_idle_event.set()`` is the wake signal."""
        while True:
            any_active = False
            for sub in list(self._streams):
                if not sub.active:
                    continue
                any_active = True
                try:
                    params = sub.get_params()
                except Exception as e:  # noqa: BLE001
                    logger.exception("subscriber %s get_params failed: %s", sub.pipeline_id, e)
                    continue
                # Bound the entire camera round-trip. The protocol's
                # internal ``_wait_image_ready`` and ``_fetch_bytes``
                # have their own timeouts (typically 30 s each), but
                # the surrounding ``aput_binx`` / ``aput_gain`` /
                # ``aput_startexposure`` calls go through ocaboxapi
                # which doesn't expose a per-call deadline. A stuck
                # TIC handler on any of those would freeze the camera
                # loop indefinitely — the same failure mode that's
                # bitten us in production. ``3 × exp_time + 15 s``
                # gives generous headroom for a healthy long exposure
                # plus camera-config overhead, and is short enough
                # that the operator sees the recovery (in logs) on
                # the same scale as their attention span. On timeout
                # we abandon this frame, sleep briefly, and loop —
                # the next iteration will retry from a fresh state.
                exp_time = float(getattr(params, "exp_time", 0.0) or 0.0)
                fetch_deadline = 3.0 * max(exp_time, 1.0) + 15.0
                try:
                    raw = await asyncio.wait_for(
                        self.submit_one(params), timeout=fetch_deadline,
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    logger.warning(
                        "subscriber %s: submit_one exceeded %.1f s "
                        "(3×exp_time + 15 s) — abandoning frame, retrying",
                        sub.pipeline_id, fetch_deadline,
                    )
                    await asyncio.sleep(1.0)
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.exception("backend submit_one failed: %s", e)
                    await asyncio.sleep(1.0)
                    continue
                # Frozen-buffer guard: a byte-identical repeat of the
                # previous frame is a stale buffer, not a new exposure —
                # dropping it here starves the solver (correct: no new
                # information) instead of letting it re-derive the same
                # correction forever. Brief sleep so a wedged driver
                # doesn't turn this loop into a busy poll.
                if self.dedup.is_duplicate(raw.array):
                    await asyncio.sleep(0.5)
                    continue
                try:
                    sub.out_queue.put_nowait(raw)
                except asyncio.QueueFull:
                    # Drop *oldest*, keep newest. Real-time guiding cares
                    # about frame currency, not throughput — stale frames
                    # at the head of the queue would be processed minutes
                    # late while the mount is already past that position.
                    # The old "drop-newest" policy was the wrong half of
                    # this trade-off.
                    try:
                        sub.out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        sub.out_queue.put_nowait(raw)
                    except asyncio.QueueFull:
                        logger.warning(
                            "subscriber %s queue still full after drain, dropping frame",
                            sub.pipeline_id,
                        )
                    else:
                        logger.debug(
                            "subscriber %s queue full, dropped oldest to keep newest",
                            sub.pipeline_id,
                        )
            if not self._streams:
                # No subscribers — exit cleanly.
                self._driver_task = None
                return
            if not any_active:
                # All subscribers paused (every pipeline in OFF mode):
                # park on the wake event instead of busy-yielding so
                # the camera (and the second observer fighting us for
                # it) gets a real break. Cleared at the top of next
                # iteration; pipeline.apply_mode() sets it on resume.
                self._idle_event.clear()
                try:
                    await self._idle_event.wait()
                except asyncio.CancelledError:
                    raise
                continue
            # Yield to event loop so other tasks can run.
            await asyncio.sleep(0)

    def wake(self) -> None:
        """Signal the drive loop to re-check subscriber states. Called
        by ``_StreamSubscription.set_active(True)`` so a resumed
        subscriber starts receiving frames immediately rather than
        waiting on the next loop iteration (which never arrives if we
        were parked on ``_idle_event``)."""
        self._idle_event.set()


@dataclass
class _StreamSubscription:
    pipeline_id: str
    out_queue: asyncio.Queue[RawFrame]
    get_params: object  # Callable[[], ExposureJob]
    active: bool = True
    # Backref to the owning collector so ``set_active`` can wake the
    # drive loop. Populated by ``subscribe_stream`` after construction.
    _collector: "CameraArrayCollector | None" = None

    def cancel(self) -> None:
        """Permanent removal from the collector — used at pipeline
        teardown. For temporary mode-OFF pauses use ``set_active``."""
        self.active = False
        if self._collector is not None:
            self._collector.wake()

    def set_active(self, active: bool) -> None:
        """Toggle frame fetching for this subscription. Used by the
        pipeline on mode-OFF / mode-ON transitions: keeps the
        subscription registered (so we don't churn collector state)
        while letting the camera idle."""
        was = self.active
        self.active = bool(active)
        if active and not was and self._collector is not None:
            self._collector.wake()
