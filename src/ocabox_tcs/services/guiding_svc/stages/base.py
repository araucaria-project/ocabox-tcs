"""Stage base contracts and frame types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np


@dataclass(frozen=True)
class RawFrame:
    """Camera output, post-protocol-decoding, pre-Stacker.

    Includes the raw pixel array plus enough provenance for Stacker to
    apply calibration (exp_time → dark current scaling) and stamp output
    files.
    """

    array: np.ndarray
    exp_time: float
    timestamp: list[int]
    roi: tuple[int, int, int, int] | None = None
    binning: int | tuple[int, int] = 1
    gain: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisFrame:
    """Stacker output, ready for Solver.

    Calibrated, stacked, masked. Saturated pixels typically NaN.
    """

    array: np.ndarray
    exp_time_total: float
    """Total integration time for the stack (sum of constituent
    exp_times)."""

    n_stacked: int
    """How many raw frames went into this analysis frame."""

    timestamp: list[int]
    """Timestamp of the *latest* constituent raw frame."""

    roi: tuple[int, int, int, int] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Stage(Protocol):
    """Marker protocol for pipeline stages.

    Stages have a managed lifecycle (`start()` / `stop()`) and consume
    from / produce to bounded asyncio.Queues. Concrete stages
    (`Stacker`, `Solver`, `Enforcer`) define their own queue interfaces;
    this base only documents the lifecycle contract.
    """

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...
