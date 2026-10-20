"""Reading a telescope's optical state through TIC — by component *kind*, never by name.

Each optical component of the graph is read according to its kind (an M3 is read as an M3 whatever
it is called); a kind without a reader contributes nothing, so its selector stays *undefined* (a
lamp without telemetry is taken as lit by the solver). Every read asks TIC for data no older than
``tolerance`` — TIC's own cache default (60 s) would serve a minute-old M3 as current — and a read
that fails proves nothing: the selector is undefined until it reads again.

Alpaca codes are mapped by ``obcom.optics.telemetry`` (the single place that knows what a cover or
shutter code means optically); the M3 port is mapped through the component's own ``positions:``,
so a port no symbol declares (oca-problems#107) is reported as an unmapped position. Whatever proves
nothing says why, for people (``ProvenState.causes``): "shutter reports Error (ShutterState 4)",
"not read (TimeoutError: …)", "controller reports an error (ErrorRaised)".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from datamodels.optics import state_key
from obcom.optics import OpticalGraph, ProvenState
from obcom.optics.telemetry import (CALIBRATOR_STATUS, COVER_STATUS, SHUTTER_STATE, calibrator_cause, cover_cause,
                                    shutter_cause)

#: ASCOM CameraState values during which the camera is using its light path
CAMERA_BUSY = {2, 3, 4}  # Exposing, Reading, Download
TERTIARY_KINDS = {'tertiaryOCA', 'tertiary'}


@dataclass(frozen=True)
class Snapshot:
    """One reading of the telescope: the proven state and the detectors busy with light."""

    state: ProvenState
    busy: Mapping[str, str]  #: detector -> why its light path must not change (exposing, state unknown)
    failed: frozenset[str]  #: components whose read failed this time


class TelescopeReader:
    """Reads the optical state of one telescope (an ``ocaboxapi.Telescope``)."""

    def __init__(self, telescope, graph: OpticalGraph, tolerance: float = 1.0,
                 sun_altitude: Callable[[], float | None] | None = None, logger: logging.Logger | None = None):
        self._tel = telescope
        self._graph = graph
        self._tolerance = tolerance
        self._sun = sun_altitude or (lambda: None)
        self._log = logger or logging.getLogger('optics')
        self._failing: set[str] = set()
        mounts = [name for name, spec in graph.components.items() if spec.kind == 'telescope']
        self._mount = mounts[0] if mounts else None

    async def read(self) -> Snapshot:
        positions: dict[str, str | None] = {}
        moving: set[str] = set()
        environment: dict[str, Any] = {}
        busy: dict[str, str] = {}
        failed: set[str] = set()
        causes: dict[str, str] = {}

        async def guarded(name: str, read: Awaitable[None]) -> None:
            try:
                await read
            except Exception as e:  # any failure of a read proves nothing about the component
                failed.add(name)
                what = 'mount not read' if name == self._mount else 'not read'
                for key in self._keys_of(name):  # a cause the device itself gave is the more precise one
                    causes.setdefault(key, f'{what} ({type(e).__name__}: {e})')
                if name not in self._failing:
                    self._log.warning(f'{name}: not read ({type(e).__name__}: {e}) — undefined until it reads again')
            else:
                if name in self._failing:
                    self._log.info(f'{name}: reads again')

        async def tertiary(name: str) -> None:
            status = await self._get(self._tel.get_tertiary(name), 'tertiarystatus')
            port, now_moving = status.get('NasmythPort'), bool(status.get('Moving'))
            declared = self._graph.node(name).spec.positions or {}
            positions[name] = next((symbol for symbol in declared if declared[symbol].port == port), None)
            if status.get('ErrorRaised'):
                positions[name], causes[name] = None, 'controller reports an error (ErrorRaised)'
            elif positions[name] is None:
                causes[name] = f'at nasmyth port {port}, which no position declares'
            if now_moving:
                moving.add(name)

        async def covercalibrator(name: str) -> None:
            component = self._tel.get_covercalibrator(name)
            cover, calibrator = await asyncio.gather(self._get(component, 'coverstate'), self._get(component, 'calibratorstate'))
            aspect = state_key(name, 'calibrator')
            positions[name], positions[aspect] = COVER_STATUS.get(cover), CALIBRATOR_STATUS.get(calibrator)
            moving.update(key for key, code in ((name, cover), (aspect, calibrator)) if code == 2)
            for key, cause in ((name, cover_cause(cover)), (aspect, calibrator_cause(calibrator))):
                if cause is not None:
                    causes[key] = cause

        async def dome(name: str) -> None:
            component = self._tel.get_dome(name)
            shutter, az = await asyncio.gather(self._get(component, 'shutterstatus'), self._get(component, 'azimuth'))
            if shutter in SHUTTER_STATE:
                environment['dome_shutter_open'] = SHUTTER_STATE[shutter]
            else:
                causes[name] = shutter_cause(shutter)
            environment['dome_az_deg'] = az

        async def mount(name: str) -> None:
            component = self._tel.get_mount(name)
            environment['mount_az_deg'], environment['mount_alt_deg'] = await asyncio.gather(
                self._get(component, 'azimuth'), self._get(component, 'altitude'))

        async def camera(name: str) -> None:
            busy[name] = 'state unknown'  # until proven idle: an unread camera may be exposing
            if await self._get(self._tel.get_camera(name), 'camerastate') in CAMERA_BUSY:
                busy[name] = 'exposing'
            else:
                del busy[name]

        reads: list[tuple[str, Awaitable[None]]] = []
        for name, node in self._graph.nodes.items():
            kind = node.spec.kind
            if kind in TERTIARY_KINDS:
                reads.append((name, tertiary(name)))
            elif kind == 'covercalibrator':
                reads.append((name, covercalibrator(name)))
            elif kind == 'dome':
                reads.append((name, dome(name)))
                if self._mount is not None:
                    reads.append((self._mount, mount(self._mount)))
            elif kind == 'camera':
                reads.append((name, camera(name)))
        await asyncio.gather(*(guarded(name, read) for name, read in reads))
        self._failing = failed

        sun = self._sun()
        if sun is not None:
            environment['sun_alt_deg'] = sun
        state = ProvenState.build(positions, moving=moving, causes=causes, **environment)
        return Snapshot(state=state, busy=busy, failed=frozenset(failed))

    def _keys_of(self, name: str) -> list[str]:
        """The state keys a component's read proves: its axes, or — for the mount — the dome's."""
        if name == self._mount:
            return [n for n, node in self._graph.nodes.items() if node.spec.kind == 'dome']
        node = self._graph.nodes.get(name)
        return [state_key(name, axis.name) for axis in node.axes] if node is not None else []

    async def _get(self, component, attribute: str):
        return await component.aget_value(attribute, time_of_data_tolerance=self._tolerance)
