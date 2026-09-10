"""MultiStarMethod — track M brightest stars via relative geometry.

Robust against single-star loss; better SNR. Matches between frames by
relative geometry (rigid translation), not ADU.

Inspired by Ekos's SEP MultiStar algorithm (default in Ekos guider).

"""

from __future__ import annotations

from typing import Any

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class MultiStarMethod:
    name = "multi_star"
    uses_adu_match = False
    produces_rotation = False

    def __init__(self, max_stars: int = 50, **params: Any) -> None:
        self.max_stars = max_stars
        self.params = params

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "MultiStarMethod.solve — detect up to self.max_stars stars; "
            "match to reference set via relative-geometry matching "
            "(point-set registration, e.g. Procrustes / nearest-neighbour "
            "with consistency check); compute median Δ as the rigid "
            "translation correction. Reference set captured on first "
            "frame after acquired transitions True. See Ekos SEP MultiStar "
            "implementation as a reference."
        )

    def reset(self) -> None:
        pass
