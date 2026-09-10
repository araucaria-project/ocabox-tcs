"""Gaussian Process regression adapter.

Non-parametric Bayesian: `y(x) ~ GP(μ(x), k(x, x'))`. Predictions and
uncertainties are closed-form in terms of the kernel and accumulated
data. Excellent for low-data regimes; native uncertainty estimation;
smooth interpolation in covariate space.

Cost: O(N³) refit / O(N²) prediction in the data buffer size N. Use a
sliding window (default 200 points) to bound it.

See `doc/auto-adjust-transformation.md` §4.3.

Implementation strategy: backend-pluggable. Default backend is
scikit-learn's `GaussianProcessRegressor` (already a transitive dep).
For larger N or autodiff needs, swap to a `gpytorch` backend.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from auto_adjust.base import Observation, Prediction
from auto_adjust.exceptions import OptionalDependencyMissing


X = TypeVar("X")


try:
    import sklearn  # type: ignore[import-not-found]  # noqa: F401
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False


class GPAdapter(Generic[X]):
    """Sliding-window Gaussian Process regression.

    **Status**: skeleton. Constructor and protocol surface defined;
    fit/predict raise NotImplementedError pending backend integration.

    Args:
        kernel: Kernel specification (string preset or backend-specific
            object). Default 'rbf+white'.
        window_size: Number of recent observations to keep in the buffer.
            Default 200. Large enough to learn the local shape; small
            enough to keep refit cheap (~50 ms for N=200, 2D input).
        backend: 'sklearn' (default) or 'gpytorch'.
        refit_every_k: Refit kernel hyperparameters every k observations
            (default 20). Cheap per-observation with frozen
            hyperparameters; expensive refit happens batched.
        name: Adapter instance name.

    Output type: scalar y. Multi-output: separate GPAdapter per output
    (independent GPs); coupled multi-output GP is a future variant.
    """

    def __init__(
        self,
        kernel: Any = "rbf+white",
        window_size: int = 200,
        backend: Literal["sklearn", "gpytorch"] = "sklearn",
        refit_every_k: int = 20,
        name: str = "gp",
    ) -> None:
        if backend == "sklearn" and not _SKLEARN_OK:
            raise OptionalDependencyMissing(
                "scikit-learn", "GPAdapter[sklearn]", install_extra="gp"
            )
        if backend == "gpytorch":
            raise NotImplementedError(
                "GPAdapter backend='gpytorch' — staged for when sliding-window "
                "sklearn refit cost becomes a bottleneck (N > ~500)."
            )
        if window_size < 10:
            raise ValueError("window_size must be ≥ 10 for sensible GP fit")
        if refit_every_k < 1:
            raise ValueError("refit_every_k must be ≥ 1")

        self.kernel_spec = kernel
        self.window_size = window_size
        self.backend = backend
        self.refit_every_k = refit_every_k
        self.name = name
        self._buffer: list[Observation[X, float]] = []
        self._n_samples: int = 0
        self._model: Any = None  # backend-specific fit result

    # -- AdaptiveTransform protocol -----------------------------------

    def predict(self, x: X) -> Prediction[float]:
        raise NotImplementedError(
            "GPAdapter.predict — call self._model.predict([x_as_array], "
            "return_std=True); wrap into Prediction. Need to define how "
            "X type is converted to the array form sklearn wants — "
            "application supplies an x_to_array adapter or X is already "
            "numeric tuple/array."
        )

    def record(self, observation: Observation[X, float]) -> None:
        raise NotImplementedError(
            "GPAdapter.record — append to self._buffer; if len > window_size, "
            "drop oldest; if self._n_samples %% self.refit_every_k == 0, "
            "trigger _refit(). Weight by observation.sigma via "
            "alpha=sigma**2 in GaussianProcessRegressor (per-point noise)."
        )

    def is_calibrated(self) -> bool:
        return self._n_samples >= max(10, 3 * self.refit_every_k)

    def reset(self) -> None:
        self._buffer.clear()
        self._n_samples = 0
        self._model = None

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "GPAdapter.serialise — pickle (self._buffer, self._model). "
            "sklearn GP models pickle cleanly; check gpytorch when that "
            "backend lands."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_samples": self._n_samples,
            "buffer_size": len(self._buffer),
            "window_size": self.window_size,
            "backend": self.backend,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        return None

    # -- Adapter-specific -----

    def _refit(self) -> None:
        raise NotImplementedError(
            "GPAdapter._refit — fit a fresh GaussianProcessRegressor "
            "from current buffer; replace self._model atomically."
        )
