"""DummyMethod — produces synthetic random corrections.

Concrete in the frame iteration. Used for end-to-end smoke tests and
to give integration partners (UIs, mount client, planner, Halina)
something to wire against before real solving lands.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.stages.base import AnalysisFrame


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class DummyMethod:
    """Returns small random corrections — visible activity for smoke
    tests but no real solving.

    Args:
        seed: RNG seed for deterministic output.
        amplitude_px: Std-dev of the random correction in pixels.
    """

    name = "dummy"
    uses_adu_match = False
    produces_rotation = False

    def __init__(self, seed: int = 1234, amplitude_px: float = 0.5, **_: Any) -> None:
        self.seed = seed
        self.amplitude_px = amplitude_px
        self._rng = np.random.default_rng(seed)
        self._n_solved = 0

    async def solve(
        self,
        frame: AnalysisFrame,
        state: dict[str, Any],
    ) -> Correction | None:
        dx = float(self._rng.normal(0, self.amplitude_px))
        dy = float(self._rng.normal(0, self.amplitude_px))
        self._n_solved += 1
        return Correction(
            dx_px=dx,
            dy_px=dy,
            method=self.name,
            confidence=0.5,
            timestamp=dt_utcnow_array(),
            metadata={"n_solved": self._n_solved, "frame_shape": list(frame.array.shape)},
        )

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._n_solved = 0
