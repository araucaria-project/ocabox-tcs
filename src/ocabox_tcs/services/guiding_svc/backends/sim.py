"""SimBackend — wraps any `CameraArrayProtocol` for in-process simulated
operation. Concrete in the frame iteration; use with FileSimProtocol.

This is the backend for end-to-end smoke tests: no NATS RPCs, no
hardware, just produces frames at request time.
"""

from __future__ import annotations

import logging

from ocabox_tcs.services.guiding_svc.protocols.base import (
    CameraArrayProtocol,
    FetchedFrame,
)


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class SimBackend:
    """File-based simulator backend.

    Args:
        protocol: A `CameraArrayProtocol` instance providing the actual
            frame source (typically `FileSimProtocol`).
    """

    def __init__(self, protocol: CameraArrayProtocol) -> None:
        self.protocol = protocol

    @property
    def name(self) -> str:
        return f"sim({self.protocol.name})"

    async def open(self) -> None:
        await self.protocol.open()
        logger.info("SimBackend opened with protocol %s", self.protocol.name)

    async def close(self) -> None:
        await self.protocol.close()

    async def submit_one(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        return await self.protocol.fetch(
            exp_time=exp_time, roi=roi, binning=binning, gain=gain
        )
