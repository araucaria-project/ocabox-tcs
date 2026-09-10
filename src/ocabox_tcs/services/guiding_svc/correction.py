"""Correction dataclass — Solver output, consumed by Controller and Enforcer.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Correction:
    """A single guiding correction produced by a Solver method.

    Attributes:
        dx_px, dy_px: **Measured error vector in sensor pixels:**
            ``star − target`` (where the light IS minus where it SHOULD
            be; target = ``guide_anchor`` for single-star hold,
            ``central_point`` for fiber). Solver methods report the raw
            error and never pre-negate: the pulse-guide model's
            ``predict()`` computes the cancelling pulse itself
            (``motion = −error`` — see ``pulse_guide.py``). A method
            that emits ``target − star`` double-negates and turns the
            guide loop into positive feedback (the 2026-07-29 fiber
            runaway, ``NIGHT_REPORT_2026-07-29_stefan.md`` §2.1).
            Regression-locked by ``tests/unit/test_correction_sign_convention.py``.
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
