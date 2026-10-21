"""Following a telescope's optical state through TIC subscriptions — by component *kind*, never by name.

Each optical component of the graph is followed according to its kind (an M3 is read as an M3
whatever it is called); a kind without a reader contributes nothing, so its selector stays
*undefined*. Every value is a TIC subscription under the Staleness Contract: the service declares
how fresh it wants the data (T1, ``tolerance``: TIC refreshes the device at that cadence, one poll
for all its subscribers), how often it wants to hear of a change at most (T0, by default the same:
a mount's encoder noise is not news every few hundred ms) and when data stops being the truth (T2,
``max_age``, default 2·T1 floored at 5 s). Past T2 TIC delivers ``None`` with the reason, and the client library does the same when
TIC itself goes silent — so a stale M3 is never taken for the current one, and the service never
bypasses TIC's cache to make sure of it.

:meth:`TelescopeReader.snapshot` turns the last deliveries into a proven state without any I/O;
:meth:`TelescopeReader.changed` waits for the next delivery. Alpaca codes are mapped by
``obcom.optics.telemetry`` (the single place that knows what a cover or shutter code means
optically); the M3 port is mapped through the component's own ``positions:``, so a port no symbol
declares (oca-problems#107) is reported as an unmapped position. Whatever proves nothing says why,
for people (``ProvenState.causes``): "shutter reports Error (ShutterState 4)", "out of date (…)",
"controller reports an error (ErrorRaised)".
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from datamodels.optics import state_key
from obcom.comunication.error_policy import ErrorPolicy, ValuePolicy
from obcom.optics import OpticalGraph, ProvenState
from obcom.optics.telemetry import (CALIBRATOR_STATUS, COVER_STATUS, SHUTTER_STATE, calibrator_cause, cover_cause,
                                    shutter_cause)

#: ASCOM CameraState values during which the camera is using its light path
CAMERA_BUSY = {2, 3, 4}  # Exposing, Reading, Download
TERTIARY_KINDS = {'tertiaryOCA', 'tertiary'}
#: a service that nurses itself through outages and declares the truth axis: past T2 a value is None
POLICY = ErrorPolicy.SERVICE.with_overrides(value_policy=ValuePolicy.NONE)


@dataclass(frozen=True)
class Snapshot:
    """The telescope as last delivered: the proven state and the detectors busy with light."""

    state: ProvenState
    busy: Mapping[str, str]  #: detector -> why its light path must not change (exposing, state unknown)
    failed: frozenset[str]  #: components with a value that proves nothing now (unread, out of date)


@dataclass(frozen=True)
class Reading:
    """One delivered value: ``None`` with ``reason`` when TIC has no truthful one."""

    value: Any
    ts: float
    reason: str | None = None


class Unread(Exception):
    """A value the snapshot needs proves nothing now; the message says why, for people."""


class TelescopeReader:
    """Follows the optical state of one telescope (an ``ocaboxapi.Telescope``) by subscriptions."""

    def __init__(self, telescope, graph: OpticalGraph, tolerance: float = 1.0, max_age: float | None = None,
                 sun_altitude: Callable[[], float | None] | None = None, logger: logging.Logger | None = None):
        self._tel = telescope
        self._graph = graph
        self._tolerance = tolerance
        self._max_age = max_age
        self._sun = sun_altitude or (lambda: None)
        self._log = logger or logging.getLogger('optics')
        mounts = [name for name, spec in graph.components.items() if spec.kind == 'telescope']
        self._mount = mounts[0] if mounts else None
        self._readings: dict[tuple[str, str], Reading] = {}
        self._subscriptions: list[tuple[Any, Any]] = []  #: (observatory, cycle query)
        self._version = 0
        self._delivered = asyncio.Event()

    # ------------------------------------------------------------------ subscriptions

    def wanted(self) -> list[tuple[str, Any, str]]:
        """(component name, ocaboxapi component, attribute) of every value the graph's kinds need."""
        out: list[tuple[str, Any, str]] = []
        for name, node in self._graph.nodes.items():
            kind = node.spec.kind
            if kind in TERTIARY_KINDS:
                out.append((name, self._tel.get_tertiary(name), 'tertiarystatus'))
            elif kind == 'covercalibrator':
                component = self._tel.get_covercalibrator(name)
                out += [(name, component, 'coverstate'), (name, component, 'calibratorstate')]
            elif kind == 'dome':
                component = self._tel.get_dome(name)
                out += [(name, component, 'shutterstatus'), (name, component, 'azimuth')]
                if self._mount is not None:
                    mount = self._tel.get_mount(self._mount)
                    out += [(self._mount, mount, 'azimuth'), (self._mount, mount, 'altitude')]
            elif kind == 'camera':
                out.append((name, self._tel.get_camera(name), 'camerastate'))
        return out

    async def start(self) -> None:
        """Subscribe to every value the graph needs; deliveries follow as TIC has them."""
        for name, component, attribute in self.wanted():
            observatory = component.observatory
            cq = await observatory.subscribe_async(f'{component.my_address()}.{attribute}',
                                                   time_of_data_tolerance=self._tolerance, max_data_age=self._max_age,
                                                   policy=POLICY, callback_method=self._delivery(name, attribute))
            self._subscriptions.append((observatory, cq))

    def _delivery(self, name: str, attribute: str) -> Callable[[list], None]:
        """The callback of one subscription (a named function: the cycle query logs its name)."""
        def deliver(responses):
            self._deliver(name, attribute, responses)
        return deliver

    def stop(self) -> None:
        for observatory, cq in self._subscriptions:
            observatory.unsubscribe(cq)
        self._subscriptions = []
        self._wake()

    def _deliver(self, name: str, attribute: str, responses) -> None:
        response = responses[0]
        if response.status and response.value is not None:
            value = response.value
            reason = None if value.v is not None else str((value.tags or {}).get('reason') or 'no truthful value')
            reading = Reading(value.v, value.ts, reason)
        else:
            error = response.error
            reading = Reading(None, time.time(), f'TIC error {error.code}: {error.message}' if error else 'TIC error')
        before = self._readings.get((name, attribute))
        if reading.reason is not None and (before is None or before.reason is None):
            self._log.warning(f'{name}.{attribute}: no truthful value ({reading.reason}) — undefined until it reads again')
        elif reading.reason is None and before is not None and before.reason is not None:
            self._log.info(f'{name}.{attribute}: reads again')
        self._readings[(name, attribute)] = reading
        self._wake()

    def _wake(self) -> None:
        self._version += 1
        self._delivered.set()
        self._delivered = asyncio.Event()

    @property
    def version(self) -> int:
        """Grows with every delivery: what :meth:`changed` waits past."""
        return self._version

    async def changed(self, since: int, timeout: float) -> int:
        """Wait until something is delivered after version ``since`` (at most ``timeout`` s); the
        version now. Any number of waiters: each keeps its own ``since``."""
        if self._version == since:
            delivered = self._delivered
            try:
                await asyncio.wait_for(delivered.wait(), timeout)
            except TimeoutError:
                pass
        return self._version

    # ------------------------------------------------------------------ the snapshot

    def snapshot(self) -> Snapshot:
        """The proven state from the last deliveries — no I/O."""
        positions: dict[str, str | None] = {}
        moving: set[str] = set()
        environment: dict[str, Any] = {}
        busy: dict[str, str] = {}
        failed: set[str] = set()
        causes: dict[str, str] = {}

        def unread(name: str, keys: list[str], e: Unread) -> None:
            failed.add(name)
            for key in keys:  # a cause the device itself gave is the more precise one
                causes.setdefault(key, str(e))

        for name, node in self._graph.nodes.items():
            kind = node.spec.kind
            if kind in TERTIARY_KINDS:
                try:
                    self._tertiary(name, positions, moving, causes)
                except Unread as e:
                    unread(name, [name], e)
            elif kind == 'covercalibrator':
                aspect = state_key(name, 'calibrator')
                for key, attribute, symbols, why in ((name, 'coverstate', COVER_STATUS, cover_cause),
                                                     (aspect, 'calibratorstate', CALIBRATOR_STATUS, calibrator_cause)):
                    try:
                        code = self._value(name, attribute)
                    except Unread as e:
                        unread(name, [key], e)
                        continue
                    positions[key] = symbols.get(code)
                    if code == 2:
                        moving.add(key)
                    elif (cause := why(code)) is not None:
                        causes[key] = cause
            elif kind == 'dome':
                try:
                    shutter = self._value(name, 'shutterstatus')
                    if shutter in SHUTTER_STATE:
                        environment['dome_shutter_open'] = SHUTTER_STATE[shutter]
                    else:
                        causes[name] = shutter_cause(shutter)
                    environment['dome_az_deg'] = self._value(name, 'azimuth')
                    if self._mount is not None:
                        environment['mount_az_deg'] = self._value(self._mount, 'azimuth', 'mount')
                        environment['mount_alt_deg'] = self._value(self._mount, 'altitude', 'mount')
                except Unread as e:
                    unread(name, [name], e)
            elif kind == 'camera':
                try:
                    if self._value(name, 'camerastate') in CAMERA_BUSY:
                        busy[name] = 'exposing'
                except Unread:
                    busy[name] = 'state unknown'  # until proven idle: an unread camera may be exposing
                    failed.add(name)

        sun = self._sun()
        if sun is not None:
            environment['sun_alt_deg'] = sun
        state = ProvenState.build(positions, moving=moving, causes=causes, **environment)
        return Snapshot(state=state, busy=busy, failed=frozenset(failed))

    def _tertiary(self, name: str, positions: dict, moving: set, causes: dict) -> None:
        status = self._value(name, 'tertiarystatus')
        port = status.get('NasmythPort')
        declared = self._graph.node(name).spec.positions or {}
        positions[name] = next((symbol for symbol in declared if declared[symbol].port == port), None)
        if status.get('ErrorRaised'):
            positions[name], causes[name] = None, 'controller reports an error (ErrorRaised)'
        elif positions[name] is None:
            causes[name] = f'at nasmyth port {port}, which no position declares'
        if status.get('Moving'):
            moving.add(name)

    def _value(self, name: str, attribute: str, what: str | None = None):
        """The last delivered value, or :class:`Unread` saying why there is none (``what`` names the
        component in the cause when it is not the one whose axis it explains: the mount of the dome)."""
        reading = self._readings.get((name, attribute))
        prefix = f'{what} ' if what else ''
        if reading is None:
            raise Unread(f'{prefix}not read yet')
        if reading.reason is not None:
            raise Unread(f'{prefix}out of date ({reading.reason})')
        return reading.value
