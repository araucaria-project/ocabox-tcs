"""Recursive Least Squares with forgetting factor.

Linear-in-parameters model `y = Φ(x) θ`. Sufficient statistics
`P_t = (Φᵀ Φ + λ I)⁻¹` updated incrementally per sample. Constant memory
(O(d²)), constant per-update compute. Forgetting factor `λ < 1` gives
exponential time-weighting so old data fades.

See `doc/auto-adjust-transformation.md` §4.2.

Implementation strategy: delegate to `padasip.filters.FilterRLS` for the
core RLS arithmetic; this adapter handles featurisation `Φ(x)`, weighting
by σ, persistence, and protocol compliance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from auto_adjust.base import Observation, Prediction
from auto_adjust.exceptions import OptionalDependencyMissing


X = TypeVar("X")


# Application-supplied feature function
FeatureFn = Callable[[X], "list[float]"]
"""Pure function `Φ(x) → feature vector` (linear-in-parameters model)."""


# Lazy import: padasip is optional. Trigger ImportError at adapter
# *import* (when this module is loaded) so configuration errors surface
# early rather than on first use.
try:
    import padasip  # type: ignore[import-not-found]  # noqa: F401
    _PADASIP_OK = True
except ImportError:
    _PADASIP_OK = False


class RLSAdapter(Generic[X]):
    """RLS with exponential forgetting.

    **Status**: skeleton. Constructor and protocol surface defined;
    update/predict raise NotImplementedError pending integration with
    `padasip.filters.FilterRLS`.

    Args:
        feature_fn: `Φ(x) → list[float]` — the linear-in-parameters
            featurisation. Determines `n` (number of parameters).
        n: Number of parameters (length of `Φ(x)` output).
        forgetting: λ ∈ (0, 1]. Default 0.99 → effective horizon ~100 obs.
        regularisation: Initial diagonal of `P` (default 1.0).
        name: Adapter instance name (for logs).

    Output type: scalar y. For multi-output, run independent RLSAdapters
    per output channel (decoupled by `Φ` — works when outputs share `x`).
    Multi-output coupled RLS would be a separate adapter (future).
    """

    def __init__(
        self,
        feature_fn: FeatureFn[X],
        n: int,
        forgetting: float = 0.99,
        regularisation: float = 1.0,
        name: str = "rls",
    ) -> None:
        if not _PADASIP_OK:
            raise OptionalDependencyMissing("padasip", "RLSAdapter", install_extra="rls")
        if not (0 < forgetting <= 1):
            raise ValueError("forgetting factor must be in (0, 1]")
        if n < 1:
            raise ValueError("n must be ≥ 1")

        self.feature_fn = feature_fn
        self.n = n
        self.forgetting = forgetting
        self.regularisation = regularisation
        self.name = name
        self._n_samples: int = 0
        self._last_residual: float | None = None
        # Lazily build the FilterRLS instance to keep import-time cheap.
        self._filter: Any = None

    def _ensure_filter(self) -> Any:
        if self._filter is None:
            import padasip as pa  # type: ignore[import-not-found]
            self._filter = pa.filters.FilterRLS(
                n=self.n, mu=self.forgetting, eps=self.regularisation
            )
        return self._filter

    # -- AdaptiveTransform protocol -----------------------------------

    def predict(self, x: X) -> Prediction[float]:
        raise NotImplementedError(
            "RLSAdapter.predict — phi = self.feature_fn(x); "
            "y = self._ensure_filter().w @ phi; "
            "wrap in Prediction with σ derived from the filter's P matrix "
            "(quadratic form phi @ P @ phi gives predictive variance)."
        )

    def record(self, observation: Observation[X, float]) -> None:
        raise NotImplementedError(
            "RLSAdapter.record — phi = self.feature_fn(observation.x); "
            "self._ensure_filter().adapt(observation.y_actual, phi); "
            "weighting by observation.sigma is non-trivial in padasip — "
            "research how to fold σ into the adaptation step (paper §1.6 / "
            "Vahidi 2005 [Vah05])."
        )

    def is_calibrated(self) -> bool:
        # Heuristic: calibrated once we have ≥ 2*n samples (rule of thumb
        # for a linear model with n parameters). Applications should
        # tune by overriding via subclass or passing a custom predicate.
        return self._n_samples >= 2 * self.n

    def reset(self) -> None:
        self._filter = None
        self._n_samples = 0
        self._last_residual = None

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "RLSAdapter.serialise — pickle (self._filter.w, self._filter.R, "
            "self._n_samples). On load: reconstruct FilterRLS and assign."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_samples": self._n_samples,
            "forgetting": self.forgetting,
            "n_parameters": self.n,
            "last_residual": self._last_residual,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        # Common case: meridian flip in a guider invalidates θ entirely.
        # Application can subscribe to its own event bus and call
        # adapter.reset() — RLSAdapter doesn't auto-reset on any event
        # by default (too domain-specific to embed here).
        return None
