"""IrisProtocol — IRIS native protocol fetch.

Placeholder; IRIS support is deferred until IRIS deployment matters.
The protocol slot exists to make the design dimension visible.
"""

from __future__ import annotations

from ocabox_tcs.services.guiding_svc.protocols.base import FetchedFrame


class IrisProtocol:
    """IRIS-speaking camera fetcher (placeholder).

    The wire format isn't HTTP — implementation will follow IRIS's
    native protocol when needed. Likely lands in `pyaraucaria.iris_image`
    or a separate IRIS-owned package, then this class wraps it.
    """

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs

    @property
    def name(self) -> str:
        return "iris(placeholder)"

    async def open(self) -> None:
        raise NotImplementedError(
            "IrisProtocol — placeholder; IRIS deployment not in scope yet."
        )

    async def close(self) -> None:
        raise NotImplementedError("IrisProtocol — placeholder.")

    async def fetch(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        raise NotImplementedError("IrisProtocol — placeholder.")
