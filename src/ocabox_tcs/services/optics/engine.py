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
  a function that has none is refused with the manual move spelled out: 4009.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from datamodels.messages import Severity
from datamodels.optics import Active, Collision, Impossible, Settable, state_key
from obcom.optics import Hold, OpticalGraph, ProvenState, check, resolve_axes, sees
from obcom.optics.check import UnknownFunction

from ocabox_tcs.tic_endpoint import Refusal

#: holder of a selector the service may not move
MANUAL = 'manual operation (not enabled for automatic actuation)'


def proven_positions(graph: OpticalGraph, state: ProvenState) -> dict[str, str]:
    """Every selector axis with a proven position — read ones and derived ones (the dome's
    open/flat/closed comes from shutter and pointing, not from a telemetry key)."""
    return {state_key(name, aspect): resolved.value
            for name in graph.selectors
            for aspect, resolved in resolve_axes(graph, state, graph.node(name)).items()
            if resolved.value is not None}


def with_busy_holds(graph: OpticalGraph, state: ProvenState, busy: Mapping[str, str]) -> ProvenState:
    """``state`` with every proven selector on the live path of a ``busy`` detector (name → why:
    exposing, state unknown) held there."""
    holds = dict(state.holds)
    positions = proven_positions(graph, state)
    for detector, why in sorted(busy.items()):
        on_path = {component for record in sees(graph, state, detector) for component in (record.terminal, *record.via)}
        for key, position in positions.items():
            if key.partition('.')[0] in on_path:
                holds.setdefault(key, Hold(position=position, holder=f'{detector} ({why})'))
    return replace(state, holds=holds)


def with_manual_holds(graph: OpticalGraph, state: ProvenState, actuatable: frozenset[str]) -> ProvenState:
    """``state`` with every proven selector outside ``actuatable`` held where it is."""
    holds = dict(state.holds)
    for key, position in proven_positions(graph, state).items():
        if key not in actuatable:
            holds.setdefault(key, Hold(position=position, holder=MANUAL))
    return replace(state, holds=holds)


def report(graph: OpticalGraph, state: ProvenState) -> dict[str, dict]:
    """Per detector: what it sees now and the verdict of each declared function (physical
    feasibility — manual moves are shown as moves, busy detectors as collisions)."""
    out = {}
    for detector in graph.detectors:
        node = graph.node(detector)
        records = sorted(sees(graph, state, detector), key=lambda r: (r.light_class, r.terminal, r.via))
        out[detector] = {
            'sees': [r.model_dump(mode='json', by_alias=True) for r in records],
            'functions': {f: check(graph, state, detector, f).model_dump(mode='json') for f in (node.paths or {})},
        }
    return out


def decide(graph: OpticalGraph, state: ProvenState, detector: str, function: str,
           actuatable: frozenset[str]) -> dict[str, str]:
    """The moves of actuatable selectors that make ``detector`` do ``function`` — empty when it
    already does — or a :class:`Refusal` that says why not, with the code a client can act on.
    ``state`` should already carry the busy holds."""
    if detector not in graph.detectors:
        raise Refusal(f'{detector!r} is not a detector of this telescope (detectors: {", ".join(graph.detectors)})', code=4007)
    try:
        verdict = check(graph, with_manual_holds(graph, state, actuatable), detector, function)
    except UnknownFunction as e:
        raise Refusal(str(e), code=4007)
    if isinstance(verdict, Active):
        return {}
    if isinstance(verdict, Settable):
        manual = {k: v for k, v in verdict.moves.items() if k not in actuatable}
        if manual:  # an undefined selector cannot be held: never move what the service may not move
            spelled = ', '.join(f'{k} → {v}' for k, v in sorted(manual.items()))
            raise Refusal(f'{detector}.{function} needs {spelled}: not enabled for automatic actuation, set it by hand')
        return dict(verdict.moves)
    if isinstance(verdict, Collision):
        if verdict.holder == MANUAL:
            raise Refusal(f'{detector}.{function} needs {verdict.selector} → {verdict.required} '
                          f'(now {verdict.held}): not enabled for automatic actuation, set it by hand')
        raise Refusal(verdict.reason, code=4008, severity=Severity.TEMPORARY)
    if isinstance(verdict, Impossible):
        raise Refusal(verdict.reason)
    raise Refusal(f'{detector}.{function}: unexpected verdict {verdict.kind}', code=2002)
