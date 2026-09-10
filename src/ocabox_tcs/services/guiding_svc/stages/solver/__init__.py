"""Solver stage — analysis frame → correction.

"""

from ocabox_tcs.services.guiding_svc.stages.solver.base import Solver, SolverMethod
from ocabox_tcs.services.guiding_svc.stages.solver.selection_policies import (
    SELECTION_POLICIES,
    SelectionPolicy,
)


__all__ = [
    "SELECTION_POLICIES",
    "SelectionPolicy",
    "Solver",
    "SolverMethod",
]
