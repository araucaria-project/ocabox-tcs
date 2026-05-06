"""DirectFetchBackend — thin wrapper that delegates to a protocol and
retries once on transient ``AlpacaFetchError``.
"""

from __future__ import annotations

import asyncio
import logging

from ocabox_tcs.services.guiding_svc._borrowed.ofp.alpaca_http import (
    AlpacaFetchError,
)
from ocabox_tcs.services.guiding_svc.protocols.base import (
    CameraArrayProtocol,
    FetchedFrame,
)


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


_RETRY_BACKOFF_S = 0.25  # short — surface persistent failures fast


class DirectFetchBackend:
    """Talks to the camera via a ``CameraArrayProtocol``.

    Args:
        protocol: Protocol instance.
        retry_once: When True (default), retry once on ``AlpacaFetchError``.
    """

    def __init__(
        self,
        protocol: CameraArrayProtocol,
        *,
        retry_once: bool = True,
    ) -> None:
        self.protocol = protocol
        self.retry_once = retry_once

    @property
    def name(self) -> str:
        return f"direct_fetch({self.protocol.name})"

    async def open(self) -> None:
        await self.protocol.open()
        logger.info("DirectFetchBackend opened: %s", self.protocol.name)

    async def close(self) -> None:
        await self.protocol.close()

    async def submit_one(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        try:
            return await self.protocol.fetch(
                exp_time=exp_time, roi=roi, binning=binning, gain=gain
            )
        except AlpacaFetchError as first:
            if not self.retry_once:
                raise
            logger.warning(
                "DirectFetchBackend: transient fetch error, retrying once: %s",
                first,
            )
            await asyncio.sleep(_RETRY_BACKOFF_S)
            try:
                return await self.protocol.fetch(
                    exp_time=exp_time, roi=roi, binning=binning, gain=gain
                )
            except AlpacaFetchError as second:
                logger.error(
                    "DirectFetchBackend: retry also failed (first=%r second=%r)",
                    first, second,
                )
                raise
