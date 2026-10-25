"""Moving selectors through TIC — by component *kind* — and proving that they moved.

Railway doctrine: *commanded ≠ proven*. A move is done only when a delivered reading (the reader's
TIC subscriptions) shows the selector at the commanded position and no longer moving; until then
the operation is in progress, and a selector that does not get there in ``prove_timeout`` fails the
operation with what it reads.

The service moves with its own TIC connection and ``request_special_permission``: the human's
permission was checked by TIC's blocker when they asked for the path. Safety cutoff still applies.

Actuators by kind: M3 (``tertiaryOCA``) selects a nasmyth port; mirror covers (``covercalibrator``)
open and close; their calibration lamp switches off, and on only at the component's configured
``calibrator_brightness`` (no guessed brightness). The dome goes **only to the flat screen**: the
mount stops tracking and slews to the screen's altitude and azimuth while the dome turns to its own
(``DomeKind.screen``); proven when the derived dome reads ``flat``. Its shutter is the observatory's
weather protection and is never touched, so the dome's other positions are out of :attr:`reach`.
Such a move waits for a person first (:mod:`.confirmation`) — the actuator only performs it.
"""
from __future__ import annotations

import asyncio
import logging
import time

from datamodels.optics import split_state_key
from obcom.optics import OpticalGraph, label, resolve_axes

from ocabox_tcs.services.optics.readings import TERTIARY_KINDS, TelescopeReader


class ActuationError(RuntimeError):
    """A move could not be commanded or was not proven."""


class Actuator:
    """Commands selector moves on one telescope and proves them with ``reader``."""

    def __init__(self, telescope, graph: OpticalGraph, reader: TelescopeReader, prove_timeout: float = 60.0,
                 logger: logging.Logger | None = None):
        self._tel, self._graph, self._reader = telescope, graph, reader
        self._prove_timeout = prove_timeout
        self._log = logger or logging.getLogger('optics')
        mounts = [name for name, spec in graph.components.items() if spec.kind == 'telescope']
        self._mount = mounts[0] if mounts else None

    @property
    def reach(self) -> dict[str, frozenset[str]]:
        """Selectors this actuator moves to some positions only: the dome to the flat screen."""
        return {name: frozenset({'flat'}) for name, node in self._graph.nodes.items() if node.spec.kind == 'dome'}

    async def apply(self, moves: dict[str, str]) -> None:
        for key, symbol in moves.items():
            await self._command(key, symbol)
        await self._prove(moves)

    async def abort(self, moves: dict[str, str]) -> None:
        """Stop what ``moves`` set in motion that can be stopped: the mount and the dome slewing to
        the flat screen. Every stop is tried; the first failure is raised after all were."""
        stops = []
        for key in moves:
            component, aspect = split_state_key(key)
            if self._graph.node(component).spec.kind == 'dome' and aspect is None:
                stops.append((f'{label(self._graph, key)} slew', self._tel.get_dome(component)))
                if self._mount is not None:
                    stops.append(('telescope slew', self._tel.get_mount(self._mount)))
        failures = []
        for what, device in stops:
            device.request_special_permission = True
            try:
                await device.aput_abortslew()
                self._log.warning(f'{what} aborted')
            except Exception as e:
                failures.append(f'{what}: {type(e).__name__}: {e}')
        if failures:
            raise ActuationError('abort failed — ' + '; '.join(failures))

    async def _command(self, key: str, symbol: str) -> None:
        component, aspect = split_state_key(key)
        spec = self._graph.node(component).spec
        what = f'{label(self._graph, key)} → {symbol}'
        if spec.kind in TERTIARY_KINDS and aspect is None:
            port = spec.positions[symbol].port
            device = self._tel.get_tertiary(component)
            put, detail = (lambda: device.aput_selectnasmythport(port)), f'nasmyth port {port}'
        elif spec.kind == 'covercalibrator' and aspect is None:
            device = self._tel.get_covercalibrator(component)
            put, detail = (device.aput_opencover if symbol == 'open' else device.aput_closecover), f'{symbol} cover'
        elif spec.kind == 'covercalibrator' and aspect == 'calibrator':
            device = self._tel.get_covercalibrator(component)
            if symbol == 'off':
                put, detail = device.aput_calibratoroff, 'calibrator off'
            else:
                brightness = (spec.model_extra or {}).get('calibrator_brightness')
                if brightness is None:
                    raise ActuationError(f'{what}: no calibrator_brightness configured for {component}')
                put, detail = (lambda: device.aput_calibratoron(brightness)), f'calibrator on at {brightness}'
        elif spec.kind == 'dome' and aspect is None and symbol == 'flat':
            await self._to_the_screen(component, what)
            return
        else:
            raise ActuationError(f'{what}: no actuator for kind {spec.kind!r}')
        device.request_special_permission = True
        self._log.info(f'{what} ({detail})')
        try:
            await put()
        except Exception as e:
            raise ActuationError(f'{what}: TIC refused the move ({type(e).__name__}: {e})') from e

    async def _to_the_screen(self, dome_name: str, what: str) -> None:
        """The mount stops tracking and slews to the flat screen while the dome turns to it."""
        node = self._graph.node(dome_name)
        target = node.kind.screen(node.spec, self._graph.components)
        if target is None or target[2] is None or self._mount is None:
            raise ActuationError(f'{what}: the flat screen position is not configured '
                                 f'(domeflat_az on the dome, domeflat_az_offset and domeflat_alt on the mount)')
        dome_az, mount_az, mount_alt = target
        mount, dome = self._tel.get_mount(self._mount), self._tel.get_dome(dome_name)
        mount.request_special_permission = dome.request_special_permission = True
        self._log.info(f'{what} (telescope to az {mount_az:g}° alt {mount_alt:g}°, dome to {dome_az:g}°)')
        try:
            await mount.aput_tracking(False)
            await asyncio.gather(mount.aput_slewtoaltaz_async(mount_az, mount_alt), dome.aput_slewtoazimuth(dome_az))
        except Exception as e:
            raise ActuationError(f'{what}: TIC refused the move ({type(e).__name__}: {e})') from e

    async def _prove(self, moves: dict[str, str]) -> None:
        timeout = self._timeout_for(moves)
        deadline = time.monotonic() + timeout
        version = self._reader.version
        while True:
            now = self._now(moves)
            pending = {k: v for k, v in moves.items() if now[k] != (v, False)}
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reads = ', '.join(f'{label(self._graph, k)} reads {now[k][0] or "nothing"}{" (moving)" if now[k][1] else ""}, wanted {v}'
                                  for k, v in pending.items())
                raise ActuationError(f'not proven within {timeout:.0f} s: {reads}')
            version = await self._reader.changed(version, remaining)

    def _now(self, moves: dict[str, str]) -> dict[str, tuple[str | None, bool]]:
        """(position, moving) of every moved axis as proven now — read or derived (the dome's flat
        comes from the pointing, not from a telemetry key)."""
        state = self._reader.snapshot().state
        now = {}
        for key in moves:
            component, aspect = split_state_key(key)
            resolved = resolve_axes(self._graph, state, self._graph.node(component)).get(aspect)
            reading = state.selectors.get(key)
            now[key] = (None if resolved is None else resolved.value, bool(reading and reading.moving))
        return now

    def _timeout_for(self, moves: dict[str, str]) -> float:
        """``prove_timeout``, or longer when a moved component (or the mount it slews) is configured
        to take longer (``slew_timeout``)."""
        names = {split_state_key(key)[0] for key in moves}
        if any(self._graph.node(name).spec.kind == 'dome' for name in names) and self._mount is not None:
            names.add(self._mount)
        configured = [float(extra['slew_timeout']) for name in names
                      if 'slew_timeout' in (extra := self._graph.components[name].model_extra or {})]
        return max([self._prove_timeout, *configured])
