# auto_adjust

Universal, application-agnostic toolkit for **auto-adjusting parametric
transformations** — online learning of a function `f : X → Y` from
empirical observations made during normal system operation.

This package provides an abstraction for the recurring
pattern documented in
[`doc/auto-adjust-transformation.md`](../../doc/auto-adjust-transformation.md):

```
Predict:   y_pred  = f(x; θ_t)
Observe:   y_actual, σ                  # measurement of real outcome
Record:    accumulate (x, y_actual, σ, t)
Update:    θ_{t+1} = U(θ_t, history)    # chosen update rule
Persist:   θ + sufficient statistics across restarts
```

## Why this lives here (for now)

The package is **deliberately application-agnostic**. It is currently
shipped under the `ocabox-tcs` repo's `pyproject.toml` for development
convenience — the first concrete user is the OCM telescope guider
(`src/ocabox_tcs/services/guiding_svc/pulse_guide.py`).

When a second consumer materialises (pointing model, focuser, exposure
calculator, …) and the API has settled, this package will be **extracted
to its own public repository** with no astronomy-specific dependencies.
Nothing in this directory should reference `ocabox_tcs`, `ocaboxapi`,
`pyaraucaria`, or telescope-specific concepts.

## Status: skeleton

The package is currently a **skeleton with empty (NotImplementedError)
adapter implementations**. The protocol, types, and stability primitives
are defined; concrete adapters are stubbed to indicate the design surface.

Implementation will follow the staging plan in
[`doc/guider/PHASES.md`](../../doc/guider/PHASES.md).

## Public API

```python
from auto_adjust import (
    AdaptiveTransform,    # Protocol
    Observation,          # input/output empirical point
    Prediction,           # predict() return type
    AdapterChain,         # composable: bootstrap → online refinement
)

from auto_adjust.adapters import (
    ActiveCalAdapter,     # one-shot bootstrap via probe + fit
    RLSAdapter,           # Recursive Least Squares with forgetting factor
    GPAdapter,            # Gaussian Process regression (sliding window)
    LocalLinearAdapter,   # kNN local-linear regression
    PolynomialAdapter,    # polynomial refit on buffer
    KalmanAdapter,        # state-space Kalman with drifting θ
)

from auto_adjust.stability import (
    DampingGuard,         # apply α · prediction
    DeadbandGuard,        # zero output below threshold
    SaturationGuard,      # clip outputs to safe limits
    OutlierGuard,         # reject samples > N σ from rolling RMS
)

from auto_adjust.events import EventBus, EventType
```

## Mapping to the design doc

Each module corresponds to a section in
[`doc/auto-adjust-transformation.md`](../../doc/auto-adjust-transformation.md):

| Module | Doc section |
|---|---|
| `base.py` | §3 Common abstraction, §6 Module interface |
| `adapters/` | §4 Method catalogue, §13 Available libraries |
| `stability.py` | §5 Stability and safety |
| `persistence.py` | §5.6 Persistent storage with staleness fingerprint |
| `events.py` | §6 Event hooks |

## Note on dependencies

`auto_adjust` only requires:

- `numpy` (always)
- `scipy` (for some adapters)
- `padasip` (optional, for `RLSAdapter` — pulls in numpy only)
- `scikit-learn` (optional, for `GPAdapter`)
- `gpytorch` (optional, for scalable GP)
- `filterpy` (optional, for `KalmanAdapter`)

Optional adapters fail at *import* with a clear message if their library
is missing, never silently. See `adapters/__init__.py`.
