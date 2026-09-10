"""Base contract for camera-array protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class FetchedFrame:
    """A single frame fetched from the camera (raw pixels + provenance)."""

    array: np.ndarray
    """2-D pixel data (typical dtype uint16, int32, or float32)."""

    exp_time: float
    """Actual exposure time used (may differ from requested if camera
    rounded)."""

    timestamp: list[int]
    """UTC timestamp of frame readout (serverish 7-element format)."""

    roi: tuple[int, int, int, int] | None = None
    """ROI used when fetching: (x, y, w, h). None = full sensor."""

    binning: int | tuple[int, int] = 1
    gain: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CameraArrayProtocol(Protocol):
    """Protocol for fetching image arrays from one camera.

    Implementations encapsulate the wire-format specifics (Alpaca HTTP
    binary, IRIS native protocol, file-based simulator, …). Used by
    `DirectFetchBackend`.

    Implementations are expected to be **session-bound to one camera**
    — multiple cameras → multiple protocol instances.
    """

    async def open(self) -> None:
        """Initialise session (open HTTP client, claim device, etc.)."""
        ...

    async def close(self) -> None:
        """Release session."""
        ...

    async def fetch(
        self,
        exp_time: float,
        roi: tuple[int, int, int, int] | None = None,
        binning: int | tuple[int, int] = 1,
        gain: int | None = None,
    ) -> FetchedFrame:
        """Fetch one frame with the given parameters."""
        ...

    @property
    def name(self) -> str:
        """Human-readable protocol identifier (for logs)."""
        ...
