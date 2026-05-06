"""Solver method registry.

Each module declares a `Method` class implementing the `SolverMethod`
protocol. Map at end of file.
"""

from ocabox_tcs.services.guiding_svc.stages.solver.methods.astrometry import AstrometryMethod
from ocabox_tcs.services.guiding_svc.stages.solver.methods.centroid import CentroidMethod
from ocabox_tcs.services.guiding_svc.stages.solver.methods.cross_correlation import (
    CrossCorrelationMethod,
)
from ocabox_tcs.services.guiding_svc.stages.solver.methods.dummy import DummyMethod
from ocabox_tcs.services.guiding_svc.stages.solver.methods.fiber_photocentroid import (
    FiberPhotocentroidMethod,
)
from ocabox_tcs.services.guiding_svc.stages.solver.methods.image_diff import ImageDiffMethod
from ocabox_tcs.services.guiding_svc.stages.solver.methods.multi_star import MultiStarMethod
from ocabox_tcs.services.guiding_svc.stages.solver.methods.multi_star_affine import (
    MultiStarAffineMethod,
)
from ocabox_tcs.services.guiding_svc.stages.solver.methods.single_star import SingleStarMethod


METHODS: dict[str, type] = {
    "dummy": DummyMethod,
    "centroid": CentroidMethod,
    "single_star": SingleStarMethod,
    "multi_star": MultiStarMethod,
    "multi_star_affine": MultiStarAffineMethod,
    "cross_correlation": CrossCorrelationMethod,
    "image_diff": ImageDiffMethod,
    "fiber_photocentroid": FiberPhotocentroidMethod,
    "astrometry": AstrometryMethod,
}


__all__ = ["METHODS"] + [cls.__name__ for cls in METHODS.values()]
