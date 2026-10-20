"""What the optics service decides — pure functions over (graph, proven state), no I/O, no clock.

The solver (``obcom.optics``) answers what each detector sees and what a function needs; this
module adds the service's operational policy on top, expressed as solver *holds* so that the
solver's own ordered fallbacks and collision reasons do the work:

- **A working detector holds its path.** Every proven selector on the live path of a detector that
  is exposing (or guiding) is held there, so a move that would change what it sees is a collision
  naming that detector — never a silent swing of M3 under an exposure. Retry later: 4008.
- **Automatic actuation is an allowlist.** Selectors the configuration does not enable (only
  ``tertiary`` to start with) are held at their proven position, so the solver prefers an
  alternative the service can realise (``dark_strict``: M3 away rather than the closed cover) and
  a function that has none is refused with the move the operator has to make spelled out: 4009.

The published verdicts and ``setpath`` come from one function, :func:`verdict`, so what a client
is shown is exactly what the service would do.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from datamodels.messages import Severity
from datamodels.optics import Active, Collision, Impossible, Settable, Verdict, split_state_key, state_key
from obcom.optics import Hold, OpticalGraph, ProvenState, check, label, resolve_axes, sees

from ocabox_tcs.tic_endpoint import Refusal

#: holder of a selector the service may not move: the operator moves it with its own controls
MANUAL = 'its own controls (not moved by the optics service)'


def selector_positions(graph: OpticalGraph, state: ProvenState) -> dict[str, str | None]:
    """Every selector axis and its proven position, ``None`` when undefined — read ones and derived
    ones (the dome's open/flat/closed comes from shutter and pointing, not from a telemetry key)."""
    return {state_key(name, aspect): resolved.value
            for name in graph.selectors
            for aspect, resolved in resolve_axes(graph, state, graph.node(name)).items()}


def with_busy_holds(graph: OpticalGraph, state: ProvenState, busy: Mapping[str, str]) -> ProvenState:
    """``state`` with every selector on the live path of a ``busy`` detector (name → why: exposing,
    state unknown) held where it is — at an unknown position when undefined."""
    holds = dict(state.holds)
    positions = selector_positions(graph, state)
    for detector, why in sorted(busy.items()):
        on_path = {component for record in sees(graph, state, detector) for component in (record.terminal, *record.via)}
        for key, position in positions.items():
            if key.partition('.')[0] in on_path:
                holds.setdefault(key, Hold(position=position, holder=f'{detector} ({why})'))
    return replace(state, holds=holds)


def with_manual_holds(graph: OpticalGraph, state: ProvenState, actuatable: frozenset[str]) -> ProvenState:
    """``state`` with every selector outside ``actuatable`` held where it is — at an unknown
    position when undefined (a broken dome shutter): the solver then routes around it."""
    holds = dict(state.holds)
    for key, position in selector_positions(graph, state).items():
        if key not in actuatable:
            holds.setdefault(key, Hold(position=position, holder=MANUAL))
    return replace(state, holds=holds)


def verdict(graph: OpticalGraph, state: ProvenState, detector: str, function: str,
            actuatable: frozenset[str]) -> Verdict:
    """The verdict of ``detector.function`` as the service would act on it: selectors outside
    ``actuatable`` are held where they are, so *settable* means "``setpath`` makes these moves" and
    a move the operator has to make is a collision held by :data:`MANUAL`, said as what to do. ``state``
    should already carry the busy holds."""
    held = with_manual_holds(graph, state, actuatable)
    result = _operators_part(graph, held, check(graph, held, detector, function))
    if isinstance(result, Settable):
        manual = sorted(k for k in result.moves if k not in actuatable)
        if manual:  # unreachable while every selector outside ``actuatable`` is held; never move one anyway
            return Impossible(kind='impossible', undefined_at=tuple(manual),
                              reason=f'{", ".join(label(graph, k) for k in manual)}: not moved by the optics service')
    return result


def _operators_part(graph: OpticalGraph, state: ProvenState, result: Verdict) -> Verdict:
    """``result`` told as what the operator does when only they can: set a part the service does not
    move with its own controls, or confirm by hand where a part is that no sensor reads."""
    if isinstance(result, Collision) and result.holder == MANUAL:
        return result.model_copy(update={'reason': f'needs {label(graph, result.selector)} {result.required}, it is {result.held}: '
                                                   f'set it with its own controls (the optics service does not move it)'})
    if isinstance(result, Impossible) and result.unavailable is None and result.undefined_at \
            and all(getattr(state.holds.get(key), 'holder', None) == MANUAL for key in result.undefined_at):
        unknown = '; '.join(f'{label(graph, key)} not known ({_cause(graph, state, key)})' for key in result.undefined_at)
        return result.model_copy(update={'reason': f'{unknown} — confirm it by hand if you know where it is'})
    return result


def _cause(graph: OpticalGraph, state: ProvenState, key: str) -> str:
    component, aspect = split_state_key(key)
    return resolve_axes(graph, state, graph.node(component))[aspect].cause or 'no reading'


def report(graph: OpticalGraph, state: ProvenState, actuatable: frozenset[str]) -> dict[str, dict]:
    """Per detector: what it sees now and the :func:`verdict` of each declared function."""
    out = {}
    for detector in graph.detectors:
        node = graph.node(detector)
        records = sorted(sees(graph, state, detector), key=lambda r: (r.light_class, r.terminal, r.via))
        out[detector] = {
            'sees': [r.model_dump(mode='json', by_alias=True) for r in records],
            'functions': {f: verdict(graph, state, detector, f, actuatable).model_dump(mode='json')
                          for f in (node.paths or {})},
        }
    return out


def require_function(graph: OpticalGraph, detector: str, function: str) -> None:
    """Refuse (4007) a request for a detector or function the graph does not declare — before
    anything else, so a typo is never answered "busy, retry later"."""
    if detector not in graph.detectors:
        raise Refusal(f'{detector!r} is not a detector of this telescope (detectors: {", ".join(graph.detectors)})', code=4007)
    declared = graph.node(detector).paths or {}
    if function not in declared:
        raise Refusal(f'{detector} declares no function {function!r} (declared: {", ".join(declared)})', code=4007)


def decide(graph: OpticalGraph, state: ProvenState, detector: str, function: str,
           actuatable: frozenset[str]) -> dict[str, str]:
    """The moves of actuatable selectors that make ``detector`` do ``function`` — empty when it
    already does — or a :class:`Refusal` that says why not, with the code a client can act on.
    ``state`` should already carry the busy holds."""
    require_function(graph, detector, function)
    result = verdict(graph, state, detector, function, actuatable)
    if isinstance(result, Active):
        return {}
    if isinstance(result, Settable):
        return dict(result.moves)
    if isinstance(result, Collision):
        if result.holder == MANUAL:
            raise Refusal(f'{detector} {function} {result.reason}')
        raise Refusal(result.reason, code=4008, severity=Severity.TEMPORARY)
    if isinstance(result, Impossible):
        raise Refusal(result.reason)
    raise Refusal(f'{detector}.{function}: unexpected verdict {result.kind}', code=2002)
