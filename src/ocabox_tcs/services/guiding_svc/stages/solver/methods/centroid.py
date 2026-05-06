"""CentroidMethod — single-star weighted centroid (sparse field, no ADU match).

"""

from __future__ import annotations

from typing import Any

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class CentroidMethod:
    """Compute centroid of a single dominant star in the subraster around
    `acquired_pos`. No multi-frame matching; assumes the brightest star
    is the right one.

    **Status**: stub.
    """

    name = "centroid"
    uses_adu_match = False
    produces_rotation = False

    def __init__(self, **params: Any) -> None:
        self.params = params

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "CentroidMethod.solve — crop subraster around state['acquired_pos'], "
            "subtract local background (median of border), compute weighted "
            "centroid (PCA via FFS.pca or simple flux-weighted COM), return "
            "delta from reference. Wide-search if state['acquired'] is False."
        )

    def reset(self) -> None:
        pass
