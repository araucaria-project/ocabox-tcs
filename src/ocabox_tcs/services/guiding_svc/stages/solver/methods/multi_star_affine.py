"""MultiStarAffineMethod — multi-star + affine fit (translation + rotation).

Useful for monitoring pipelines that build a pointing model. Detects
field rotation in addition to translation.

"""

from __future__ import annotations

from typing import Any

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class MultiStarAffineMethod:
    name = "multi_star_affine"
    uses_adu_match = False
    produces_rotation = True

    def __init__(self, max_stars: int = 50, **params: Any) -> None:
        self.max_stars = max_stars
        self.params = params

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "MultiStarAffineMethod.solve — same as MultiStarMethod up to "
            "the matching step; instead of medianing translation deltas, "
            "fit an affine transformation (Procrustes, scipy / opencv "
            "estimateAffine2D, or analytic SVD on centred coords). Output "
            "translation (dx, dy) AND rotation (drot_rad). Enforcer "
            "applies translation only; rotation reported in Correction "
            "for downstream consumers (pointing model)."
        )

    def reset(self) -> None:
        pass
