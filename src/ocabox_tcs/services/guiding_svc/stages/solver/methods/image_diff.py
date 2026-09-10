"""ImageDiffMethod — image differencing + peak.

Cheaper than cross-correlation but less robust. Planned method; stub
only.

"""

from __future__ import annotations

from typing import Any

import numpy as np

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class ImageDiffMethod:
    name = "image_diff"
    uses_adu_match = False
    produces_rotation = False

    def __init__(self, **params: Any) -> None:
        self.params = params
        self._ref_image: np.ndarray | None = None

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "ImageDiffMethod.solve — diff = frame.array - self._ref_image; "
            "find brightest residual; sub-pixel localise; return as "
            "correction. Cheap but sensitive to non-translational changes "
            "(transparency, sky flat-fielding). Planned method per Mirek's "
            "GuidDiff."
        )

    def reset(self) -> None:
        self._ref_image = None
