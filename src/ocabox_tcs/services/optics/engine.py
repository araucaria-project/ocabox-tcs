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
  An enabled selector the actuator reaches only some positions of (the dome: to the flat screen,
  never the shutter) is held as well whenever the solver would need another one.
- **Some moves wait for a person** (:mod:`.confirmation`): a settable verdict names them in
  ``confirm``, and ``setpath`` starts them only once a person has confirmed.
- **Light that comes by itself** (the sky at dusk) makes a function *not yet* possible; ``setpath``
  then makes the moves that prepare the path for it.

The published verdicts and ``setpath`` come from one function, :func:`verdict`, so what a client
is shown is exactly what the service would do.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from datamodels.messages import Severity
from datamodels.optics import Active, Collision, Impossible, Settable, Verdict, split_state_key, state_key
from obcom.optics import Hold, OpticalGraph, ProvenState, check, label, resolve_axes, sees

from ocabox_tcs.services.optics.confirmation import ConfirmPolicy
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


#: no confirmation beyond the floor, every actuatable selector reached at every position
_NO_POLICY = ConfirmPolicy()


@dataclass(frozen=True)
class Decision:
    """What ``setpath`` does: the moves (none when the detector already does the function), the
    moves among them that wait for a person, and the light still to come they prepare for."""

    moves: dict[str, str]
    waiting: tuple[str, ...] = ()
    awaits: str | None = None


def _moves_of(result: Verdict) -> Mapping[str, str]:
    if isinstance(result, Settable):
        return result.moves
    if isinstance(result, Impossible):
        return result.prepare
    return {}


def verdict(graph: OpticalGraph, state: ProvenState, detector: str, function: str,
            actuatable: frozenset[str], policy: ConfirmPolicy = _NO_POLICY,
            reach: Mapping[str, frozenset[str]] | None = None) -> Verdict:
    """The verdict of ``detector.function`` as the service would act on it: selectors outside
    ``actuatable`` are held where they are, so *settable* means "``setpath`` makes these moves" and
    a move the operator has to make is a collision held by :data:`MANUAL`, said as what to do.
    ``reach`` limits an actuatable selector to the positions its actuator can make (the dome: only
    to the flat screen) — a route needing another one is decided again with it held, so the solver
    takes an alternative or says what the operator does. A settable verdict names in ``confirm`` the
    moves that wait for a person (``policy``). ``state`` should already carry the busy holds."""
    reach = reach or {}
    movable = actuatable
    while True:
        held = with_manual_holds(graph, state, movable)
        result = _operators_part(graph, held, check(graph, held, detector, function), movable)
        out_of_reach = {k for k, v in _moves_of(result).items() if k in reach and v not in reach[k]}
        if not out_of_reach:
            break
        movable = movable - out_of_reach
    if isinstance(result, Settable):
        manual = sorted(k for k in result.moves if k not in movable)
        if manual:  # unreachable while every selector outside ``actuatable`` is held; never move one anyway
            return Impossible(kind='impossible', undefined_at=tuple(manual),
                              reason=f'{", ".join(label(graph, k) for k in manual)}: not moved by the optics service')
        return result.model_copy(update={'confirm': policy.waiting(graph, state, result.moves)})
    return result


def _operators_part(graph: OpticalGraph, state: ProvenState, result: Verdict, actuatable: frozenset[str]) -> Verdict:
    """``result`` told as what the operator does when only they can: set a part the service does not
    move with its own controls (saying what a position nobody selects takes: the dome's flat is the
    telescope at the screen), or confirm by hand what no sensor reads (a part's position, a lamp)."""
    if isinstance(result, Collision) and result.holder == MANUAL:
        component, aspect = split_state_key(result.selector)
        node = graph.node(component)
        meaning = node.kind.meaning(node.spec, graph.components, aspect, result.required)
        needs = f'needs {label(graph, result.selector)} {result.required}'
        if meaning is not None:
            return result.model_copy(update={'reason': f'{needs} — {meaning}; it is {result.held} now, '
                                                       f'the optics service does not move it'})
        return result.model_copy(update={'reason': f'{needs}, it is {result.held}: set it with its own controls '
                                                   f'(the optics service does not move it)'})
    if isinstance(result, Impossible) and result.unavailable is None and result.undefined_at \
            and all(getattr(state.holds.get(key), 'holder', None) == MANUAL for key in result.undefined_at):
        unknown = '; '.join(f'{label(graph, key)} not known ({_cause(graph, state, key)})' for key in result.undefined_at)
        return result.model_copy(update={'reason': f'{unknown} — confirm it by hand if you know where it is'})
    if isinstance(result, Impossible) and result.undefined_at and not set(result.undefined_at) & actuatable:
        return result.model_copy(update={'reason': f'{result.reason} — if you know, confirm it by hand'})
    return result


def _cause(graph: OpticalGraph, state: ProvenState, key: str) -> str:
    component, aspect = split_state_key(key)
    return resolve_axes(graph, state, graph.node(component))[aspect].cause or 'no reading'


def report(graph: OpticalGraph, state: ProvenState, actuatable: frozenset[str], policy: ConfirmPolicy = _NO_POLICY,
           reach: Mapping[str, frozenset[str]] | None = None) -> dict[str, dict]:
    """Per detector: what it sees now and the :func:`verdict` of each declared function."""
    out = {}
    for detector in graph.detectors:
        node = graph.node(detector)
        records = sorted(sees(graph, state, detector), key=lambda r: (r.light_class, r.terminal, r.via))
        out[detector] = {
            'sees': [r.model_dump(mode='json', by_alias=True) for r in records],
            # defaults left out: a client fills them in, and one older than a field meets it only when it is used
            'functions': {f: verdict(graph, state, detector, f, actuatable, policy, reach).model_dump(mode='json', exclude_defaults=True)
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
           actuatable: frozenset[str], policy: ConfirmPolicy = _NO_POLICY,
           reach: Mapping[str, frozenset[str]] | None = None) -> Decision:
    """What ``setpath`` does for ``detector.function``: the moves of actuatable selectors — none
    when it already does it — and which of them wait for a person; for light still to come (the sky
    at dusk) the moves that prepare the path. Otherwise a :class:`Refusal` that says why not, with
    the code a client can act on. ``state`` should already carry the busy holds."""
    require_function(graph, detector, function)
    result = verdict(graph, state, detector, function, actuatable, policy, reach)
    if isinstance(result, Active):
        return Decision({})
    if isinstance(result, Settable):
        return Decision(dict(result.moves), tuple(result.confirm))
    if isinstance(result, Impossible) and result.awaits is not None:
        return Decision(dict(result.prepare), policy.waiting(graph, state, result.prepare), result.awaits)
    if isinstance(result, Collision):
        if result.holder == MANUAL:
            raise Refusal(f'{detector} {function} {result.reason}')
        raise Refusal(result.reason, code=4008, severity=Severity.TEMPORARY)
    if isinstance(result, Impossible):
        raise Refusal(result.reason)
    raise Refusal(f'{detector}.{function}: unexpected verdict {result.kind}', code=2002)
