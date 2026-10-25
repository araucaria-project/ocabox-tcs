"""Moves that wait for a person — which ones, and the request that waits (ocabox-tcs#33).

Some selector moves move something big in the dome: the telescope and the dome going to the flat
screen, the mirror covers swinging by day when people work up there. The service makes them only
after a person has confirmed exactly those moves, from TOI, the CLI, or any client that answers the
optics journal's actions. Which moves wait is policy:

- **a floor in code** — the dome (``dome → flat`` means the mount *and* the dome slewing to the
  screen) always; the mirror covers by day, both ways (nobody opens them in daylight except for
  maintenance, and anything large that moves can hit a head);
- **additions from the configuration** (``confirm: {key: always | day}``), never removals.

*By day* is the sky's own state: ``day`` per the sun's altitude, or not known (no sun position), which
counts as day — the safe side.

A :class:`Pending` is what the person confirms: the moves, in words, and the state they were decided
from. The service decides again at confirmation; if anything differs the request is *superseded* and
has to be asked again, so a person never confirms a plan they were not shown.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from datamodels.optics import split_state_key
from obcom.optics import OpticalGraph, ProvenState, label, resolve_axes


class Need(str, Enum):
    """When a move of a state key waits for a person."""

    ALWAYS = 'always'
    DAY = 'day'  #: while the sky is day, or its state is not known


#: The floor, by component kind and aspect: configuration adds to it, never takes away.
FLOOR: Mapping[tuple[str, str | None], Need] = {
    ('dome', None): Need.ALWAYS,  # the mount and the dome slew to the flat screen
    ('covercalibrator', None): Need.DAY,  # the mirror covers swing; the calibration lamp is no moving part
}


@dataclass(frozen=True)
class ConfirmPolicy:
    """Which moves wait for a person on one telescope: the :data:`FLOOR` plus ``extra`` from the
    configuration (state key → :class:`Need`)."""

    extra: Mapping[str, Need] = field(default_factory=dict)

    @classmethod
    def from_config(cls, graph: OpticalGraph, configured: Mapping[str, str] | None) -> ConfirmPolicy:
        """From the service configuration's ``confirm:``; a key or a need it does not know is an error
        at start, never a move made unconfirmed."""
        extra: dict[str, Need] = {}
        axes = graph.state_axes
        for key, need in (configured or {}).items():
            if key not in axes:
                raise ValueError(f'confirm: {key!r} is no state key of this telescope (keys: {", ".join(sorted(axes))})')
            try:
                extra[key] = Need(need)
            except ValueError:
                raise ValueError(f'confirm: {key}: {need!r} is not one of {", ".join(n.value for n in Need)}') from None
        return cls(extra)

    def need(self, graph: OpticalGraph, key: str) -> Need | None:
        component, aspect = split_state_key(key)
        floor = FLOOR.get((graph.node(component).spec.kind, aspect))
        extra = self.extra.get(key)
        if Need.ALWAYS in (floor, extra):
            return Need.ALWAYS
        return floor or extra

    def waiting(self, graph: OpticalGraph, state: ProvenState, moves: Mapping[str, str]) -> tuple[str, ...]:
        """The keys of ``moves`` that wait for a person now, in move order."""
        day = is_day(graph, state)
        return tuple(key for key in moves
                     if (need := self.need(graph, key)) is Need.ALWAYS or (need is Need.DAY and day))


def is_day(graph: OpticalGraph, state: ProvenState) -> bool:
    """The sky is day — or its state is not known, which counts as day (the safe side)."""
    skies = [node for node in graph.nodes.values() if node.spec.kind == 'sky']
    if not skies:
        return True
    return any(resolve_axes(graph, state, sky)[None].value in (None, 'day') for sky in skies)


def says(graph: OpticalGraph, moves: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    """The waiting moves in words, as the person confirms them: "the telescope at the flat screen:
    dome at 0°, telescope at az 180° alt 15°", "mirror covers open"."""
    lines = []
    for key in keys:
        component, aspect = split_state_key(key)
        node = graph.node(component)
        meaning = node.kind.meaning(node.spec, graph.components, aspect, moves[key])
        lines.append(meaning or f'{label(graph, key)} {moves[key]}')
    return lines


def fingerprint(graph: OpticalGraph, state: ProvenState, moves: Mapping[str, str],
                busy: Mapping[str, str]) -> tuple:
    """What a confirmation is about: the moves, where the moved axes are now, and which detectors
    are busy — equal fingerprints mean the person confirms exactly what they were shown."""
    now = []
    for key in sorted(moves):
        component, aspect = split_state_key(key)
        resolved = resolve_axes(graph, state, graph.node(component)).get(aspect)
        now.append((key, None if resolved is None else resolved.value))
    return tuple(sorted(moves.items())), tuple(now), tuple(sorted(busy))


@dataclass
class Pending:
    """A light-path change that waits for a person: what was asked, what waits, until when."""

    detector: str
    function: str
    moves: dict[str, str]
    waiting: tuple[str, ...]
    says: list[str]
    fingerprint: tuple
    asked_by: str | None
    timeout: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    asked_at: float = field(default_factory=time.time)
    conversation: object | None = None  #: the journal entry announcing it, when one was published

    @property
    def expires_at(self) -> float:
        return self.asked_at + self.timeout

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    def same_request(self, detector: str, function: str, moves: Mapping[str, str]) -> bool:
        return (detector, function, dict(moves)) == (self.detector, self.function, self.moves)

    def published(self) -> dict:
        """The ``operation`` document while it waits."""
        return {'id': self.id, 'detector': self.detector, 'function': self.function, 'moves': self.moves,
                'caller': self.asked_by, 'state': 'awaiting_confirmation',
                'confirm': {'keys': list(self.waiting), 'says': self.says, 'expires_at': self.expires_at}}
