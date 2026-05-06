"""Correction dataclass — Solver output, consumed by Controller and Enforcer.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Correction:
    """A single guiding correction produced by a Solver method.

    Attributes:
        dx_px, dy_px: Translation correction in pixel space.
        drot_rad: Field rotation observed (None if method doesn't measure
            it). Reported but typically not enforced by Enforcer (no
            rotator on most OCM mounts).
        method: Name of the Solver method that produced this correction.
        confidence: 0.0..1.0; method-specific quality score.
        n_stars_used: For multi-star methods.
        fwhm_px: Mean FWHM at solve time.
        timestamp: UTC timestamp [Y, M, D, h, m, s, μs] (serverish format).
        metadata: Free-form provenance.
    """

    dx_px: float
    dy_px: float
    method: str
    confidence: float
    timestamp: list[int]

    drot_rad: float | None = None
    dscale: float | None = None
    n_stars_used: int | None = None
    fwhm_px: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
