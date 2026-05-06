"""Concrete adapter implementations.

Each adapter implements the `AdaptiveTransform` protocol from
`auto_adjust.base`. Adapters are intentionally **stubs** at this stage —
the public surface (constructor signature, methods) is defined, but
internal logic raises `NotImplementedError`. This is the agreed approach:
the design is encoded in the shape, implementation follows in staged
phases.

Optional dependencies fail at import time of the *specific adapter*, not
at package-import. So you can use the package even if e.g. `padasip` is
absent — you just can't import `RLSAdapter`.
"""

from auto_adjust.adapters.active_cal import ActiveCalAdapter
from auto_adjust.adapters.fixed import FixedLinearAdapter
from auto_adjust.adapters.gp import GPAdapter
from auto_adjust.adapters.kalman import KalmanAdapter
from auto_adjust.adapters.local_linear import LocalLinearAdapter
from auto_adjust.adapters.polynomial import PolynomialAdapter
from auto_adjust.adapters.rls import RLSAdapter


__all__ = [
    "ActiveCalAdapter",
    "FixedLinearAdapter",
    "GPAdapter",
    "KalmanAdapter",
    "LocalLinearAdapter",
    "PolynomialAdapter",
    "RLSAdapter",
]
