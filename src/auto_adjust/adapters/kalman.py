"""Kalman filter adapter — state-space treatment of drifting θ.

`θ_t = θ_{t-1} + w_t` (process noise), `y_t = H_t θ_t + v_t`
(measurement noise). Standard Kalman update.

Pros: principled drift handling, native uncertainty propagation.
Cons: complex setup, needs explicit process-noise model.

See `doc/auto-adjust-transformation.md` §4.6.

**Status**: skeleton. Listed for completeness; implement when application
genuinely calls for state-space treatment (pulse-guide model probably
doesn't, focuser thermal-drift might).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from auto_adjust.base import Observation, Prediction
from auto_adjust.exceptions import OptionalDependencyMissing


X = TypeVar("X")


try:
    import filterpy  # type: ignore[import-not-found]  # noqa: F401
    _FILTERPY_OK = True
except ImportError:
    _FILTERPY_OK = False


# Application-supplied measurement matrix builder
MeasurementMatrixFn = Callable[[X], "list[list[float]]"]
"""Build H_t (the measurement matrix) for a given input x. For
linear-in-parameters models with phi(x), H_t is just phi(x).T as a row
vector."""


class KalmanAdapter(Generic[X]):
    """Kalman filter with drifting state θ and observation y = H_t θ + v_t.

    Args:
        measurement_matrix_fn: Build H_t from x.
        n: State dimension.
        process_noise_cov: Q matrix (drift model). Default = small
            diagonal (very slow drift).
        measurement_noise_cov: R matrix (observation noise). Per-call σ
            from `Observation.sigma` may override; this is the prior.
        name: Adapter instance name.
    """

    def __init__(
        self,
        measurement_matrix_fn: MeasurementMatrixFn[X],
        n: int,
        process_noise_cov: Any = None,
        measurement_noise_cov: Any = None,
        name: str = "kalman",
    ) -> None:
        if not _FILTERPY_OK:
            raise OptionalDependencyMissing(
                "filterpy", "KalmanAdapter", install_extra="kalman"
            )
        if n < 1:
            raise ValueError("n must be ≥ 1")
        self.measurement_matrix_fn = measurement_matrix_fn
        self.n = n
        self.process_noise_cov = process_noise_cov
        self.measurement_noise_cov = measurement_noise_cov
        self.name = name
        self._kf: Any = None
        self._n_samples = 0

    def predict(self, x: X) -> Prediction[float]:
        raise NotImplementedError(
            "KalmanAdapter.predict — H = self.measurement_matrix_fn(x); "
            "y = H @ self._kf.x; sigma from H @ P @ H.T."
        )

    def record(self, observation: Observation[X, float]) -> None:
        raise NotImplementedError(
            "KalmanAdapter.record — H = ...; "
            "self._kf.predict(); self._kf.update(observation.y_actual, "
            "R=observation.sigma**2). Build self._kf lazily with "
            "filterpy.kalman.KalmanFilter."
        )

    def is_calibrated(self) -> bool:
        return self._kf is not None and self._n_samples >= 2 * self.n

    def reset(self) -> None:
        self._kf = None
        self._n_samples = 0

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "KalmanAdapter.serialise — pickle (self._kf.x, self._kf.P, "
            "self._n_samples)."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_samples": self._n_samples,
            "n_state": self.n,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        return None
