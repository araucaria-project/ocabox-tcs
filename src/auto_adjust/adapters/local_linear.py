"""Local linear / kNN regression adapter.

For each query `x`, fit a small linear model on the k nearest neighbours
in the buffer. Simple, no global model, handles local nonlinearity.

See `doc/auto-adjust-transformation.md` §4.4.

**Status**: skeleton.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from auto_adjust.base import Observation, Prediction


X = TypeVar("X")


class LocalLinearAdapter(Generic[X]):
    """kNN local-linear regression.

    Args:
        k: Number of neighbours used for the local fit.
        window_size: Maximum buffer size (oldest observations dropped).
        distance: Distance function `(x_a, x_b) -> float`. If None,
            assumes X is a numeric tuple/list and uses Euclidean.
        name: Adapter instance name.
    """

    def __init__(
        self,
        k: int = 8,
        window_size: int = 500,
        distance: Any = None,
        name: str = "local_linear",
    ) -> None:
        if k < 2:
            raise ValueError("k must be ≥ 2 for a linear local fit")
        if window_size < k:
            raise ValueError("window_size must be ≥ k")
        self.k = k
        self.window_size = window_size
        self.distance = distance
        self.name = name
        self._buffer: list[Observation[X, float]] = []
        self._n_samples = 0

    def predict(self, x: X) -> Prediction[float]:
        raise NotImplementedError(
            "LocalLinearAdapter.predict — find k nearest neighbours by "
            "self.distance, fit weighted least squares (weights ∝ 1/dist²), "
            "evaluate at x. Use scipy.spatial.cKDTree if X is numeric for "
            "O(log N) neighbour query; otherwise linear scan."
        )

    def record(self, observation: Observation[X, float]) -> None:
        raise NotImplementedError(
            "LocalLinearAdapter.record — append to buffer; trim to "
            "window_size; invalidate any cached kdtree."
        )

    def is_calibrated(self) -> bool:
        return self._n_samples >= 2 * self.k

    def reset(self) -> None:
        self._buffer.clear()
        self._n_samples = 0

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "LocalLinearAdapter.serialise — pickle self._buffer and config."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_samples": self._n_samples,
            "buffer_size": len(self._buffer),
            "k": self.k,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        return None
