"""Active-calibration adapter — bootstrap by probing the system.

Pattern: deliberately apply known inputs, observe outputs, fit `θ`
analytically (or via a small batch optimisation) from the resulting
mini-experiment. The classic auto-guider calibration procedure.

Use case: produces a strong initial `θ` quickly, but doesn't track drift
between calibrations. Pair with an online refinement adapter via
`AdapterChain` for production use.

See `doc/auto-adjust-transformation.md` §4.1 and §7.2 stage A.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from auto_adjust.base import Observation, Prediction
from auto_adjust.exceptions import NotCalibratedError


X = TypeVar("X")
Y = TypeVar("Y")


# Application-supplied callbacks
ProbeRunner = Callable[[], Awaitable[list[Observation]]]
"""Async callable that runs the probe sequence and returns the observed
(input, output) pairs. Application owns the side effects (sending
commands to the system, measuring responses)."""

ParameterFitter = Callable[[list[Observation]], Any]
"""Pure function: given the probe observations, return the parameter
vector `θ` (any application-defined type)."""

Predictor = Callable[[Any, Any], Any]
"""Pure function: given `(θ, x)`, return predicted `y`."""


class ActiveCalAdapter(Generic[X, Y]):
    """Bootstrap adapter — fits `θ` once from a probe sequence, then
    serves predictions statically until reset.

    **Status**: skeleton. Constructor and protocol surface defined;
    `calibrate()` and `predict()` raise NotImplementedError until concrete
    fitter and predictor logic are wired in.

    Args:
        probe_runner: Application-supplied async callable that runs the
            probe sequence (e.g. send +RA, -RA, +Dec, -Dec pulses) and
            returns the resulting observations.
        fitter: Pure function that solves for `θ` from probe observations.
        predictor: Pure function that produces `y` given `(θ, x)`.
        name: Adapter instance name (for logs).

    See `doc/auto-adjust-transformation.md` §7.2 stage A for the
    pulse-guide-specific instantiation; the pattern generalises.
    """

    def __init__(
        self,
        probe_runner: ProbeRunner,
        fitter: ParameterFitter,
        predictor: Predictor,
        name: str = "active_cal",
    ) -> None:
        self.probe_runner = probe_runner
        self.fitter = fitter
        self.predictor = predictor
        self.name = name
        self._theta: Any | None = None
        self._n_probe_observations: int = 0

    # -- AdaptiveTransform protocol -----------------------------------

    def predict(self, x: X) -> Prediction[Y]:
        if self._theta is None:
            raise NotCalibratedError(
                f"{self.name}: no θ — run calibrate() first"
            )
        # Once concrete predictor is plugged in, this becomes a simple
        # callable. Stub status indicates the gluework is the next step.
        raise NotImplementedError(
            "ActiveCalAdapter.predict — wrap self.predictor(self._theta, x) "
            "into a Prediction and return. Sigma estimation strategy: "
            "carry the residual σ from the probe fit."
        )

    def record(self, observation: Observation[X, Y]) -> None:
        # Active calibration is bootstrap-only — observations during
        # normal operation are ignored. Pair with an online adapter via
        # AdapterChain to get continuous refinement.
        return None

    def is_calibrated(self) -> bool:
        return self._theta is not None

    def reset(self) -> None:
        self._theta = None
        self._n_probe_observations = 0

    def serialise(self) -> bytes:
        raise NotImplementedError(
            "ActiveCalAdapter.serialise — pickle (self._theta, "
            "self._n_probe_observations) or equivalent. Application-defined "
            "θ type means we can't pick a format here."
        )

    def health_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calibrated": self.is_calibrated(),
            "n_probe_observations": self._n_probe_observations,
        }

    def notify_event(self, event: str, **payload: Any) -> None:
        # Active cal is a one-shot bootstrap. Recalibration on
        # configuration change is the application's job (call reset()
        # then calibrate() again). Events are recorded for diagnostics
        # but don't drive automatic action here.
        return None

    # -- Adapter-specific lifecycle -----------------------------------

    async def calibrate(self) -> None:
        """Run the probe sequence and fit `θ`.

        Stub status: ties together `probe_runner` → `fitter` and stores
        the result. The orchestration is mechanical; left as
        NotImplementedError to defer the question of:
            - what to do on probe failure (partial success? abort?)
            - how to expose probe progress to the application
            - how to compose with `EventBus.publish(CALIBRATION_STARTED)`
        """
        raise NotImplementedError(
            "ActiveCalAdapter.calibrate — orchestrate "
            "self.probe_runner() → self.fitter(observations) → store θ. "
            "Decide failure semantics (atomic / partial / event-driven) "
            "before implementing."
        )
