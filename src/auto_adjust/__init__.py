"""auto_adjust — universal toolkit for auto-adjusting parametric transformations.

See README.md and doc/auto-adjust-transformation.md (in the host repo) for
design rationale and method catalogue.

Public API: protocol, types, stability primitives, persistence helpers,
event types. Concrete adapter implementations are in `auto_adjust.adapters`.
"""

from auto_adjust.base import (
    AdapterChain,
    AdaptiveTransform,
    Observation,
    Prediction,
)
from auto_adjust.events import EventBus, EventType
from auto_adjust.exceptions import (
    AdapterError,
    NotCalibratedError,
    OptionalDependencyMissing,
)
from auto_adjust.persistence import (
    EnvironmentFingerprint,
    PersistedState,
)
from auto_adjust.stability import (
    DampingGuard,
    DeadbandGuard,
    OutlierGuard,
    SaturationGuard,
    StabilityChain,
)


__version__ = "0.0.1.dev0"

__all__ = [
    # Core
    "AdaptiveTransform",
    "AdapterChain",
    "Observation",
    "Prediction",
    # Events
    "EventBus",
    "EventType",
    # Exceptions
    "AdapterError",
    "NotCalibratedError",
    "OptionalDependencyMissing",
    # Persistence
    "EnvironmentFingerprint",
    "PersistedState",
    # Stability
    "DampingGuard",
    "DeadbandGuard",
    "OutlierGuard",
    "SaturationGuard",
    "StabilityChain",
]
