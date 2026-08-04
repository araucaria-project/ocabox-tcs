"""Pluggable focus-calculation methods.

A *method* consumes the set of FITS files accumulated so far by a job
and produces a (possibly partial) :class:`MethodResult`. Several
methods may run within one job on the same files — the job publishes
one result message per method per update.

Two families are anticipated:

- **Batch / V-curve methods** (available now): sharpness is measured
  per frame, a curve is fitted over (focus, sharpness) pairs, the
  extremum is the answer. Every new file simply re-fits over the
  fuller dataset — "iterative improvement" falls out for free. These
  wrap :class:`pyaraucaria.focus.Focus` (the algorithms TOI uses
  today: ``rms``, ``rms_quad``, ``fwhm``, ``laplacian``,
  ``lorentzian``).
- **Dynamic-search methods** (stub): minimisation-style algorithms
  that after each frame *suggest* the next focuser position (or
  declare convergence) instead of requiring a pre-planned scan. The
  protocol carries the suggestion; the algorithm is future work.

``pyaraucaria`` is an optional dependency (extra ``focus``); the
module imports without it, and the registry simply reports the batch
methods as unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])

try:  # optional dependency (extra: focus)
    from pyaraucaria.focus import Focus as _PyaFocus
except ImportError:  # pragma: no cover - exercised only without extras
    _PyaFocus = None


@dataclass
class Suggestion:
    """Advice for the client driving the focuser.

    ``kind``:
        - ``"next_position"`` — try this focuser position next
          (dynamic-search methods; ``position`` is set),
        - ``"stop"`` — the method believes it has converged; further
          frames won't improve the result,
        - ``"none"`` — no advice (typical for batch methods mid-scan).
    """

    kind: str = "none"
    position: float | None = None


@dataclass
class MethodResult:
    """One method's answer after ingesting the files seen so far.

    ``status``:
        - ``"partial"`` — not enough data for a trustworthy fit yet
          (or fit quality below par); ``best_focus`` may still carry
          a provisional value,
        - ``"ok"`` — converged / fit healthy,
        - ``"failed"`` — method cannot produce a result from this
          dataset (and says why in ``message``).

    ``fit`` carries method-specific curve data for plotting (TOI's
    focus window reads the same shape: ``focus_values``,
    ``sharpness_values``, ``fit_x``, ``fit_y``, ``coef``).
    """

    method: str
    status: str
    best_focus: float | None = None
    files_used: int = 0
    suggestion: Suggestion = field(default_factory=Suggestion)
    fit: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "best_focus": self.best_focus,
            "files_used": self.files_used,
            "suggestion": {
                "kind": self.suggestion.kind,
                "position": self.suggestion.position,
            },
            "fit": self.fit,
            "message": self.message,
        }


@runtime_checkable
class FocusMethod(Protocol):
    """The contract every focus algorithm implements.

    ``update`` is called with the *complete* list of files available
    to the job so far (paths, ordered by arrival). Methods are free to
    cache per-file measurements internally — the job guarantees a
    method instance lives for the whole job and sees a monotonically
    growing list.
    """

    name: str
    #: Method can improve its answer as files trickle in.
    iterative: bool
    #: Method emits ``next_position`` suggestions (dynamic search).
    suggests_next: bool

    async def update(self, files: list[str], params: dict[str, Any]) -> MethodResult:
        ...


class VCurveMethod:
    """Batch V-curve fit over all files seen so far — adapter around
    :meth:`pyaraucaria.focus.Focus.calculate` (the exact code TOI runs
    today, list-of-files form).

    Focus positions come from the FITS header (``focus_keyword``,
    default ``FOCUS``). Re-fits from scratch on every update; per-file
    sharpness caching is a later optimisation (pyaraucaria recomputes
    internally).
    """

    iterative = True
    suggests_next = False

    def __init__(self, name: str) -> None:
        if _PyaFocus is None:
            raise RuntimeError(
                "pyaraucaria not installed — install extras: "
                "pip install ocabox-tcs[focus]"
            )
        if name not in _PyaFocus.METHODS:
            raise ValueError(f"unknown pyaraucaria focus method: {name!r}")
        self.name = name
        #: Minimum frames before a fit is attempted (polynomial degree
        #: requirement lives in pyaraucaria; this is the cheap
        #: pre-check so partials don't spam warnings).
        self._min_files = int(_PyaFocus.METHODS[name].get("deg", 2)) + 1

    async def update(self, files: list[str], params: dict[str, Any]) -> MethodResult:
        if len(files) < self._min_files:
            return MethodResult(
                method=self.name, status="partial", files_used=len(files),
                message=f"waiting for data: {len(files)}/{self._min_files} frames",
            )
        loop = asyncio.get_running_loop()
        try:
            # Focus.calculate is CPU/IO-bound synchronous code —
            # keep the service loop responsive.
            result = await loop.run_in_executor(
                None,
                lambda: _PyaFocus.calculate(
                    fits_path=list(files),
                    method=self.name,
                    focus_keyword=params.get("focus_keyword", "FOCUS"),
                    crop=int(params.get("crop", 10)),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — method failure is a result, not a crash
            return MethodResult(
                method=self.name, status="failed", files_used=len(files),
                message=str(exc),
            )
        if result is None:
            return MethodResult(
                method=self.name, status="failed", files_used=len(files),
                message="pyaraucaria returned no result",
            )
        best, meta = result
        status = "ok" if meta.get("status") == "ok" else "partial"
        return MethodResult(
            method=self.name,
            status=status,
            best_focus=float(best),
            files_used=len(files),
            fit={
                "coef": _aslist(meta.get("coef")),
                "focus_values": _aslist(meta.get("focus_values")),
                "sharpness_values": _aslist(meta.get("sharpness_values")),
                "fit_x": _aslist(meta.get("fit_x")),
                "fit_y": _aslist(meta.get("fit_y")),
            },
        )


class DynamicSearchStub:
    """Placeholder for minimisation-style dynamic focus search.

    Target behaviour: after each frame propose the next focuser
    position (``Suggestion(kind="next_position")``), converging like a
    golden-section / parabolic line search, and emit
    ``Suggestion(kind="stop")`` once the bracket is tight. The client
    executes the suggestions — the service never moves hardware.

    Not implemented — registered so the name and the wire contract are
    stable from day one; selecting it yields honest ``failed`` results.
    """

    name = "dynamic_search"
    iterative = True
    suggests_next = True

    async def update(self, files: list[str], params: dict[str, Any]) -> MethodResult:
        return MethodResult(
            method=self.name, status="failed", files_used=len(files),
            message="dynamic_search is not implemented yet",
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PYA_METHOD_NAMES = ("rms", "rms_quad", "fwhm", "laplacian", "lorentzian")


def available_methods() -> dict[str, str]:
    """Name → short availability note, for discovery metrics."""
    out: dict[str, str] = {}
    for name in _PYA_METHOD_NAMES:
        out[name] = "ok" if _PyaFocus is not None else "unavailable (install ocabox-tcs[focus])"
    out[DynamicSearchStub.name] = "stub (not implemented)"
    return out


def create_method(name: str) -> FocusMethod:
    """Instantiate a method by name. Raises ``ValueError`` for unknown
    names and ``RuntimeError`` when the backing library is missing —
    the job turns either into a per-method ``failed`` result."""
    if name == DynamicSearchStub.name:
        return DynamicSearchStub()
    if name in _PYA_METHOD_NAMES:
        return VCurveMethod(name)
    raise ValueError(f"unknown focus method: {name!r}")


def _aslist(value: Any) -> list[Any] | None:
    if value is None:
        return None
    try:
        return [float(v) for v in value]
    except TypeError:
        return None
