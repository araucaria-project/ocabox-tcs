"""Base contract for camera-array collector backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


@runtime_checkable
class CollectorBackend(Protocol):
    """Where frames come from for one camera. Owns the hardware/transport
    session and is consumed by `CameraArrayCollector` to satisfy
    `ExposureJob` requests.

    Two flavours of submission are supported (mirroring the architecture
    document):
      • `submit_one(...)` — single-shot fetch (snapshot, calib).
      • `subscribe_stream(...)` — continuous fetch loop (guiding/monitoring).

    For the frame iteration, only `submit_one` is required to be wired;
    `subscribe_stream` may delegate to a simple loop.
    """

    async def open(self) -> None:
        """Initialise the backend (open transport session, etc.)."""
        ...

    async def close(self) -> None:
        """Tear down."""
        ...

    async def submit_one(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        """Single-shot fetch. Blocks until result available."""
        ...

    @property
    def name(self) -> str:
        ...
