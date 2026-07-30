"""Unit tests for FrameDeduplicator + the collector's frozen-buffer guard.

Regression for the 2026-07-29 incident: a frozen camera buffer was
re-served for 10.5 h with fresh timestamps, producing a bit-identical
correction that walked the mount blind (~7 000 pulses). See
``doc/guider/NIGHT_REPORT_2026-07-29_stefan.md`` §2.3.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.camera_array_collector import (
    CameraArrayCollector,
    ExposureJob,
    FrameDeduplicator,
)
from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


def _noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(loc=100.0, scale=5.0, size=(64, 64)).astype(np.float32)


# ---------------------------------------------------------------------------
# FrameDeduplicator
# ---------------------------------------------------------------------------


def test_first_frame_is_never_duplicate() -> None:
    dedup = FrameDeduplicator()
    assert dedup.is_duplicate(_noise(0)) is False


def test_identical_content_is_duplicate() -> None:
    dedup = FrameDeduplicator()
    frozen = _noise(0)
    assert dedup.is_duplicate(frozen) is False
    # Same content in a NEW array object — content, not identity, decides.
    assert dedup.is_duplicate(frozen.copy()) is True
    assert dedup.duplicate_run == 1
    assert dedup.duplicates_total == 1


def test_fresh_noise_frames_are_not_duplicates() -> None:
    """Real frames always differ (photon/read noise) — no false trips."""
    dedup = FrameDeduplicator()
    for seed in range(20):
        assert dedup.is_duplicate(_noise(seed)) is False
    assert dedup.duplicates_total == 0


def test_recovery_resets_the_run() -> None:
    dedup = FrameDeduplicator()
    frozen = _noise(0)
    dedup.is_duplicate(frozen)
    for _ in range(5):
        assert dedup.is_duplicate(frozen.copy()) is True
    assert dedup.duplicate_run == 5
    # Camera recovers:
    assert dedup.is_duplicate(_noise(1)) is False
    assert dedup.duplicate_run == 0
    assert dedup.duplicates_total == 5


def test_frozen_camera_escalates_to_error(caplog) -> None:
    dedup = FrameDeduplicator(alert_after=3)
    frozen = _noise(0)
    dedup.is_duplicate(frozen)
    with caplog.at_level(logging.WARNING, logger="camera_array_collector"):
        for _ in range(3):
            dedup.is_duplicate(frozen.copy())
    messages = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FROZEN" in r.message for r in messages)


def test_shape_change_is_not_duplicate() -> None:
    """Same bytes, different geometry (ROI/binning change) → new frame."""
    dedup = FrameDeduplicator()
    flat = np.zeros((64, 64), dtype=np.float32)
    assert dedup.is_duplicate(flat) is False
    assert dedup.is_duplicate(flat.reshape(32, 128)) is False


# ---------------------------------------------------------------------------
# Collector integration — duplicates never reach the pipeline queue
# ---------------------------------------------------------------------------


class _ScriptedBackend:
    """Backend stub replaying a scripted list of arrays."""

    name = "scripted"

    def __init__(self, arrays: list[np.ndarray]) -> None:
        self._arrays = list(arrays)
        self.fetches = 0

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def submit_one(self, exp_time, roi=None, binning=1, gain=None) -> FetchedFrame:
        if not self._arrays:
            # Script exhausted — park forever; the test cancels us.
            await asyncio.sleep(3600)
        self.fetches += 1
        return FetchedFrame(
            array=self._arrays.pop(0),
            exp_time=exp_time,
            timestamp=dt_utcnow_array(),
        )


@pytest.mark.asyncio
async def test_drive_loop_drops_duplicate_frames() -> None:
    frozen = _noise(0)
    backend = _ScriptedBackend([frozen, frozen.copy(), frozen.copy(), _noise(1)])
    collector = CameraArrayCollector("cam", backend)
    await collector.open()

    out_q: asyncio.Queue = asyncio.Queue(maxsize=8)
    job = ExposureJob(pipeline_id="mon", exp_time=0.01)
    collector.subscribe_stream("mon", out_q, get_params=lambda: job)
    try:
        # 4 scripted frames; dedup sleeps 0.5 s per duplicate.
        await asyncio.sleep(1.5)
    finally:
        await collector.close()

    delivered = []
    while not out_q.empty():
        delivered.append(out_q.get_nowait())
    # First frozen frame + the fresh one; the two stale repeats dropped.
    assert len(delivered) == 2
    assert collector.dedup.duplicates_total == 2
