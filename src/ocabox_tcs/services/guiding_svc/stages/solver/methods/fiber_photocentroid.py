"""FiberPhotocentroidMethod — weighted COM in the residual flux around
a known fiber position.

Spectrograph fiber feed scenario: most of the target's light goes into
the fiber; only a halo / annulus is visible on the detector. Compute
photocentroid of that residual to determine which side of the fiber the
star is biased toward.

Operates on residual flux directly — does NOT use individual star
detection (`uses_adu_match = False`).

"""

from __future__ import annotations

from typing import Any

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class FiberPhotocentroidMethod:
    name = "fiber_photocentroid"
    uses_adu_match = False
    produces_rotation = False

    def __init__(
        self,
        fiber_radius_px: float = 5.0,
        analysis_radius_px: float = 20.0,
        **params: Any,
    ) -> None:
        self.fiber_radius_px = fiber_radius_px
        self.analysis_radius_px = analysis_radius_px
        self.params = params

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "FiberPhotocentroidMethod.solve — \n"
            "  centre = state['central_point']  # known fiber position\n"
            "  crop annulus [fiber_radius_px, analysis_radius_px] around centre\n"
            "  subtract local background\n"
            "  weighted COM of remaining flux\n"
            "  → (dx, dy) bias from fiber. Sign convention: dx>0 means \n"
            "  star is biased to +x relative to fiber centre.\n"
            "  Caveat: when guiding is perfect, residual ADU is very low; \n"
            "  noise dominates → method should report low confidence in \n"
            "  that regime."
        )

    def reset(self) -> None:
        pass
