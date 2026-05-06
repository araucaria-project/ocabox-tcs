"""Stability and safety primitives.

Composable guards that wrap or interpose on adapter outputs to prevent
the closed-loop pathologies described in
`doc/auto-adjust-transformation.md` §5: oscillation from over-correction,
runaway from numerical pathology, polluting the model with outliers.

These are deliberately implemented (not stubbed) — the logic is small,
universal, and benefits from being shared across applications.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Generic, TypeVar


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Damping
# ---------------------------------------------------------------------------


class DampingGuard:
    """Apply `α · y_pred` instead of `y_pred`, with `α ∈ (0, 1]`.

    Common pattern: ramp `alpha` from `alpha_min` (cold start) to
    `alpha_max` (converged) as the adapter accumulates successful
    observations. The ramp criterion is application-defined; this guard
    provides the multiplier and the ramp helper but doesn't decide when
    to step.

    Apply scalar damping to numeric outputs; for vector outputs, scale
    each component identically.
    """

    def __init__(self, alpha_min: float = 0.5, alpha_max: float = 1.0) -> None:
        if not (0 < alpha_min <= alpha_max <= 1):
            raise ValueError("require 0 < alpha_min ≤ alpha_max ≤ 1")
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.alpha = alpha_min

    def ramp_to(self, alpha: float) -> None:
        """Set `alpha` directly (clipped to [alpha_min, alpha_max])."""
        self.alpha = max(self.alpha_min, min(self.alpha_max, alpha))

    def ramp_by_progress(self, progress: float) -> None:
        """Linearly interpolate alpha by `progress ∈ [0, 1]`.

        progress=0 → alpha_min, progress=1 → alpha_max.
        """
        progress = max(0.0, min(1.0, progress))
        self.alpha = self.alpha_min + progress * (self.alpha_max - self.alpha_min)

    def apply(self, y: float) -> float:
        return self.alpha * y

    def apply_vec(self, y: Iterable[float]) -> list[float]:
        return [self.alpha * v for v in y]


# ---------------------------------------------------------------------------
# Deadband
# ---------------------------------------------------------------------------


class DeadbandGuard:
    """If |x| < threshold, return zero (the system isn't worth poking).

    Applied to the input side (request) — not the prediction.

    Use to suppress twitchy behaviour from noise-only error signals.
    """

    def __init__(self, threshold: float) -> None:
        if threshold < 0:
            raise ValueError("threshold must be ≥ 0")
        self.threshold = threshold

    def passes(self, x: float) -> bool:
        return abs(x) >= self.threshold

    def apply_scalar(self, x: float) -> float:
        return x if self.passes(x) else 0.0

    def apply_norm(self, x: Iterable[float]) -> list[float] | None:
        """Apply to a vector by Euclidean norm: zero out the whole vector
        if its norm is below threshold."""
        xv = list(x)
        norm = sqrt(sum(v * v for v in xv))
        return xv if norm >= self.threshold else [0.0] * len(xv)


# ---------------------------------------------------------------------------
# Saturation / clipping
# ---------------------------------------------------------------------------


@dataclass
class SaturationGuard:
    """Clip outputs to safe limits.

    Set `lo`/`hi` per scalar component. For vector outputs, use one guard
    per axis. `apply_clipped` returns the clipped value AND a boolean
    indicating whether clipping occurred (caller may want to log).
    """

    lo: float = float("-inf")
    hi: float = float("inf")

    def apply(self, y: float) -> float:
        return max(self.lo, min(self.hi, y))

    def apply_clipped(self, y: float) -> tuple[float, bool]:
        clipped = max(self.lo, min(self.hi, y))
        return clipped, clipped != y


# ---------------------------------------------------------------------------
# Outlier rejection
# ---------------------------------------------------------------------------


class OutlierGuard:
    """Reject samples whose residual exceeds N rolling-RMS standard
    deviations of recent residuals.

    Application records the observed residual (pred − actual) via
    `record_residual`; queries `is_outlier(residual)` before recording
    a new observation in the adapter.

    The rolling RMS is computed over `window` recent values; until the
    window has at least `min_samples` entries, all residuals are accepted
    (cold-start tolerance).
    """

    def __init__(
        self,
        n_sigma: float = 5.0,
        window: int = 50,
        min_samples: int = 5,
    ) -> None:
        if n_sigma <= 0:
            raise ValueError("n_sigma must be > 0")
        if window < 1 or min_samples < 1 or min_samples > window:
            raise ValueError("require 1 ≤ min_samples ≤ window")
        self.n_sigma = n_sigma
        self.window = window
        self.min_samples = min_samples
        self._recent: deque[float] = deque(maxlen=window)

    def record_residual(self, residual: float) -> None:
        if isfinite(residual):
            self._recent.append(residual)

    def rms(self) -> float | None:
        if len(self._recent) < self.min_samples:
            return None
        s = sum(r * r for r in self._recent)
        return sqrt(s / len(self._recent))

    def is_outlier(self, residual: float) -> bool:
        rms = self.rms()
        if rms is None or rms == 0:
            return False
        return abs(residual) > self.n_sigma * rms

    def reset(self) -> None:
        self._recent.clear()


# ---------------------------------------------------------------------------
# Composable chain
# ---------------------------------------------------------------------------


class StabilityChain(Generic[T]):
    """Compose multiple guards. Provided for convenience; applications
    can also wire guards by hand for finer control.

    Currently a thin pass-through — left as a structure indicator. Real
    composition logic gets added when concrete patterns repeat across
    applications. (Implementing this as the same composition for every
    use case would constrain too early — see PHASES.md.)
    """

    def __init__(self) -> None:
        self.damping: DampingGuard | None = None
        self.deadband: DeadbandGuard | None = None
        self.saturation: list[SaturationGuard] = []
        self.outlier: OutlierGuard | None = None
