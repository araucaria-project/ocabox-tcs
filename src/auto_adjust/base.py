"""Core protocol and types for auto-adjusting transformations.

These types are the *contract* every adapter must satisfy. Application code
holds an `AdaptiveTransform` reference (the protocol), not a concrete
adapter, so swapping implementations is mechanical.

See `doc/auto-adjust-transformation.md` §3 (common abstraction) and §6
(module interface) for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable


X = TypeVar("X")
Y = TypeVar("Y")


@dataclass(frozen=True)
class Observation(Generic[X, Y]):
    """An empirical (input, output) pair with provenance.

    Adapters consume a stream of these via `record()`. The pair represents
    "we asked the system to do `x`, and the actual outcome was `y_actual`,
    measured with uncertainty `sigma`".

    Attributes:
        x: Input value (request + conditions). Type is application-defined
            (often a tuple, dict, or NumPy array).
        y_actual: Observed actual output. Type is application-defined.
        sigma: Measurement uncertainty in output units. Use 1.0 for
            unweighted observations. Adapters that support weighted updates
            divide error contribution by sigma².
        timestamp: UNIX timestamp of the observation. Used for forgetting /
            time-based weighting. 0.0 = unknown.
        metadata: Optional free-form provenance (e.g. which sensor produced
            the measurement). Not used by adapters; pass-through for logs.
    """

    x: X
    y_actual: Y
    sigma: float = 1.0
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prediction(Generic[Y]):
    """A prediction returned by `AdaptiveTransform.predict()`.

    Attributes:
        y: The predicted output value.
        sigma: Predictive uncertainty (in output units). Standard deviation
            of the predictive distribution at this `x`. Methods that don't
            natively produce uncertainty estimate it heuristically (e.g.
            from recent residuals).
        is_calibrated: Whether the underlying adapter judges its prediction
            trustworthy at this point. False during cold-start or in sparse
            covariate regions.
        metadata: Optional adapter-specific diagnostics (e.g. the `k`
            neighbours used by a local method, kernel hyperparameters
            employed).
    """

    y: Y
    sigma: float
    is_calibrated: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AdaptiveTransform(Protocol[X, Y]):
    """Protocol satisfied by every adapter.

    Implementations live in `auto_adjust.adapters`. The protocol
    intentionally does not specify *how* observations are stored, *when*
    updates fire, or *what* model class is used — those are implementation
    details that vary across adapters (RLS, GP, polynomial refit, …).

    Threading: implementations should document their concurrency contract.
    The recommended default is "single-threaded; caller serialises access".
    """

    def predict(self, x: X) -> Prediction[Y]:
        """Compute the predicted output for a new input.

        Raises `NotCalibratedError` if the adapter is not yet calibrated
        and the caller hasn't opted into low-confidence predictions.
        """
        ...

    def record(self, observation: Observation[X, Y]) -> None:
        """Record an empirical observation.

        May immediately update internal parameters (online adapters) or
        only buffer the point for later refit (batch-style adapters).
        Implementations should respect any active stability guards
        (outlier rejection, etc.) and may raise `EmpiricalPointRejected`.
        """
        ...

    def is_calibrated(self) -> bool:
        """Whether predictions are currently trustworthy."""
        ...

    def reset(self) -> None:
        """Discard learnt parameters; revert to default initialisation."""
        ...

    def serialise(self) -> bytes:
        """Pickle-or-equivalent dump of parameters and sufficient
        statistics, suitable for cross-restart persistence.

        Implementations should also include enough metadata to detect
        staleness on load (n_samples, last update timestamp, hyperparams).
        """
        ...

    def health_metrics(self) -> dict[str, Any]:
        """Diagnostic snapshot.

        Recommended keys:
            n_samples: int                  number of recorded observations
            last_update_ts: float | None    UNIX time of last parameter update
            recent_residual_rms: float | None  RMS of last K predictions
            confidence: float | None        adapter-specific quality 0..1
        Adapter-specific extra keys are encouraged.
        """
        ...

    def notify_event(self, event: str, **payload: Any) -> None:
        """Inform the adapter of external context changes that might
        invalidate part of its state (e.g. focus shift, meridian flip,
        instrument reconfiguration).

        Standard event names live in `auto_adjust.events.EventType`.
        Adapters silently ignore events they don't care about.
        """
        ...


# ---------------------------------------------------------------------------
# Composable chain
# ---------------------------------------------------------------------------


class AdapterChain(Generic[X, Y]):
    """Composable adapter that delegates `predict()` to whichever child
    is ready, and routes `record()` to all children.

    Typical use case: chain a one-shot **bootstrap** (e.g.
    `ActiveCalAdapter` that fits θ from a probe sequence) with an
    **online refinement** stage (e.g. `RLSAdapter` that updates θ from
    every subsequent observation).

    The chain serves predictions from the first child whose
    `is_calibrated()` is True, falling back to the next. New observations
    are always forwarded to all children — they may use them differently
    (the bootstrap may discard, the online may update).

    This class is **not** stubbed — it's a thin orchestrator and the
    semantics are clear from the protocol alone.
    """

    def __init__(self, *children: AdaptiveTransform[X, Y]) -> None:
        if not children:
            raise ValueError("AdapterChain requires at least one child adapter")
        self._children = list(children)

    @property
    def children(self) -> list[AdaptiveTransform[X, Y]]:
        return list(self._children)

    def predict(self, x: X) -> Prediction[Y]:
        # Reverse order: a typical chain is [bootstrap, online_refinement];
        # once online_refinement is calibrated we prefer its prediction.
        for child in reversed(self._children):
            if child.is_calibrated():
                return child.predict(x)
        # No child calibrated — return the first child's prediction with
        # its is_calibrated=False; caller decides whether to use it.
        return self._children[0].predict(x)

    def record(self, observation: Observation[X, Y]) -> None:
        for child in self._children:
            child.record(observation)

    def is_calibrated(self) -> bool:
        return any(child.is_calibrated() for child in self._children)

    def reset(self) -> None:
        for child in self._children:
            child.reset()

    def serialise(self) -> bytes:
        # The chain itself doesn't have parameters — concatenate child
        # serialisations with a length-prefixed framing. Concrete framing
        # is left to a future implementation; current stub raises so the
        # design hole is visible.
        raise NotImplementedError(
            "AdapterChain serialise/load not implemented yet — design hole. "
            "Children should typically be persisted independently."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "children": [child.health_metrics() for child in self._children],
            "calibrated_child_index": next(
                (i for i, c in enumerate(self._children) if c.is_calibrated()),
                None,
            ),
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        for child in self._children:
            child.notify_event(event, **payload)
