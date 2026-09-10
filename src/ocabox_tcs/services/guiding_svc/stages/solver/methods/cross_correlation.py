"""CrossCorrelationMethod — image-to-reference correlation peak.

No individual star detection — correlates the current subraster (or
full image) with a stored reference. Robust in crowded fields and for
nebulae as guide targets.

freshness).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


class CrossCorrelationMethod:
    name = "cross_correlation"
    uses_adu_match = False
    produces_rotation = False

    def __init__(self, **params: Any) -> None:
        self.params = params
        self._ref_image: np.ndarray | None = None

    async def solve(
        self, frame: AnalysisFrame, state: dict[str, Any]
    ) -> Correction | None:
        raise NotImplementedError(
            "CrossCorrelationMethod.solve — \n"
            "  if self._ref_image is None: capture frame.array as ref, "
            "    request Controller to mark acquired=True, return None.\n"
            "  else: correlate frame.array vs self._ref_image (FFT-based, "
            "    scipy.signal.fftconvolve or skimage.registration.phase_cross_correlation), "
            "    locate sub-pixel peak, output (dx, dy). \n"
            "  Reference freshness: refresh on \n"
            "  notify_event('focus_changed' | 'filter_changed' | "
            "  'set_reference')."
        )

    def reset(self) -> None:
        self._ref_image = None
