"""FixedLinearAdapter — a non-learning ``AdaptiveTransform`` that applies
a fixed 2×2 linear map ``y = M @ x``.

The trivial case of an adaptive parametric transformation (parameters are
constants). Domain-agnostic: ``x`` and ``y`` are 2-tuples of floats; the
caller decides what they mean (pulse durations, displacements, voltages,
…). For the inverse-Jacobian use case, instantiate ``M`` with the inverse
of the forward map.

When a real learning adapter (RLS, GP) is later swapped in, the
``AdaptiveTransform`` interface is identical, so callers don't change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from auto_adjust.base import Observation, Prediction


@dataclass
class FixedLinearAdapter:
    """Fixed 2×2 linear ``AdaptiveTransform``: ``y = [[m11, m12], [m21, m22]] @ x``.

    Args:
        m11, m12, m21, m22: Matrix entries.
        sigma: Predictive uncertainty surfaced in ``Prediction.sigma``.
    """

    m11: float
    m12: float
    m21: float
    m22: float
    sigma: float = 0.0

    name: str = field(default="fixed_linear", init=False)
    _n_observed: int = field(default=0, init=False)
    _last_record_ts: float | None = field(default=None, init=False)

    # -- AdaptiveTransform interface -----------------------------------

    def predict(self, x: tuple[float, float]) -> Prediction[tuple[float, float]]:
        x1, x2 = float(x[0]), float(x[1])
        y1 = self.m11 * x1 + self.m12 * x2
        y2 = self.m21 * x1 + self.m22 * x2
        return Prediction(
            y=(y1, y2),
            sigma=self.sigma,
            is_calibrated=True,
            metadata={
                "m11": self.m11, "m12": self.m12,
                "m21": self.m21, "m22": self.m22,
            },
        )

    def record(
        self, observation: Observation[tuple[float, float], tuple[float, float]]
    ) -> None:
        self._n_observed += 1
        if observation.timestamp:
            self._last_record_ts = observation.timestamp

    def is_calibrated(self) -> bool:
        return True

    def reset(self) -> None:
        self._n_observed = 0
        self._last_record_ts = None

    def serialise(self) -> bytes:
        payload = {
            "m11": self.m11, "m12": self.m12,
            "m21": self.m21, "m22": self.m22,
            "sigma": self.sigma,
            "n_observed": self._n_observed,
            "last_record_ts": self._last_record_ts,
        }
        return json.dumps(payload).encode("utf-8")

    def health_metrics(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "calibrated": True,
            "m11": self.m11, "m12": self.m12,
            "m21": self.m21, "m22": self.m22,
            "n_samples": self._n_observed,
            "last_update_ts": self._last_record_ts,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        return None
