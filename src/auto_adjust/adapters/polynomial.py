"""Polynomial regression adapter with periodic refit.

Choose a polynomial form `f(x; θ)` (e.g., 2nd order in two covariates),
refit `θ` on the full buffer every K samples or T seconds. Simple to
specify, gives interpretable `θ`, weighted refit by σ is straightforward.

See `doc/auto-adjust-transformation.md` §4.5.

**Status**: skeleton.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from auto_adjust.base import Observation, Prediction


X = TypeVar("X")


# Application-supplied feature function — same role as in RLSAdapter
FeatureFn = Callable[[X], "list[float]"]


class PolynomialAdapter(Generic[X]):
    """Linear-in-θ regression with periodic refit on a buffer.

    Mathematically equivalent to RLSAdapter under the right conditions
    (linear model with weighted least squares); differs in **memory and
    refit timing**: PolynomialAdapter keeps the raw buffer and refits
    occasionally, rather than maintaining sufficient statistics
    incrementally.

    Args:
        feature_fn: `Φ(x) → feature vector`.
        n: Number of parameters (length of `Φ(x)` output).
        window_size: Buffer size (oldest dropped).
        refit_every_k: Refit after every k new observations. Default 10.
        name: Adapter instance name.
    """

    def __init__(
        self,
        feature_fn: FeatureFn[X],
        n: int,
        window_size: int = 500,
        refit_every_k: int = 10,
        name: str = "polynomial",
    ) -> None:
        if n < 1 or window_size < n or refit_every_k < 1:
            raise ValueError("require n ≥ 1, window_size ≥ n, refit_every_k ≥ 1")
        self.feature_fn = feature_fn
        self.n = n
        self.window_size = window_size
        self.refit_every_k = refit_every_k
        self.name = name
        self._buffer: list[Observation[X, float]] = []
        self._theta: Any = None  # numpy array after first fit
        self._n_samples = 0

    def predict(self, x: X) -> Prediction[float]:
        raise NotImplementedError(
            "PolynomialAdapter.predict — phi = self.feature_fn(x); "
            "y = phi @ self._theta; estimate σ from buffer residuals."
        )

    def record(self, observation: Observation[X, float]) -> None:
        raise NotImplementedError(
            "PolynomialAdapter.record — append to buffer (trim to "
            "window_size); if self._n_samples %% self.refit_every_k == 0 "
            "or self._theta is None, call self._refit()."
        )

    def is_calibrated(self) -> bool:
        return self._theta is not None and self._n_samples >= 2 * self.n

    def reset(self) -> None:
        self._buffer.clear()
        self._theta = None
        self._n_samples = 0

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "PolynomialAdapter.serialise — pickle (self._theta, "
            "self._buffer, self._n_samples)."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_samples": self._n_samples,
            "buffer_size": len(self._buffer),
            "n_parameters": self.n,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        return None

    def _refit(self) -> None:
        raise NotImplementedError(
            "PolynomialAdapter._refit — build design matrix Φ from "
            "buffer, weight rows by 1/σ_i, solve weighted least squares "
            "via numpy.linalg.lstsq or scipy.linalg.lstsq."
        )
