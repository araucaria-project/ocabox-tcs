"""AstrometryMethod — astrometric solve for recovery and pointing-model fits.

Slow but most robust: compute WCS for the frame, compare to reference
WCS, derive (dx, dy, drot) in pixel space.

Use cases:
  - Recovery after star-lost (no priors needed)
  - Monitoring pipeline feeding pointing model

B13 / D5 discussion).
"""

from __future__ import annotations

from typing import Any

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class AstrometryMethod:
    name = "astrometry"
    uses_adu_match = False
    produces_rotation = True

    def __init__(
        self,
        solver: str = "astrometry.net",
        timeout_s: float = 30.0,
        **params: Any,
    ) -> None:
        self.solver = solver
        self.timeout_s = timeout_s
        self.params = params

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "AstrometryMethod.solve — invoke astrometry.net (local solve-field "
            "binary) or equivalent service via subprocess; parse WCS from "
            "the resulting .wcs file; compare to reference WCS to derive "
            "translation + rotation. Slow (1-30s per solve) — typical use "
            "is recovery after star_lost, not every frame. Application "
            "rate-limits via state.frequency."
        )

    def reset(self) -> None:
        pass
