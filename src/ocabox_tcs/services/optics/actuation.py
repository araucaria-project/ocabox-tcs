"""Moving selectors through TIC — by component *kind* — and proving that they moved.

Railway doctrine: *commanded ≠ proven*. A move is done only when a delivered reading (the reader's
TIC subscriptions) shows the selector at the commanded position and no longer moving; until then
the operation is in progress, and a selector that does not get there in ``prove_timeout`` fails the
operation with what it reads.

The service moves with its own TIC connection and ``request_special_permission``: the human's
permission was checked by TIC's blocker when they asked for the path. Safety cutoff still applies.

Actuators by kind: M3 (``tertiaryOCA``) selects a nasmyth port; mirror covers (``covercalibrator``)
open and close; their calibration lamp switches off, and on only at the component's configured
``calibrator_brightness`` (no guessed brightness). The dome is not an optical lever — its shutter is
the observatory's weather protection and its flat position moves the mount — so it has no actuator.
"""
from __future__ import annotations

import logging
import time

from datamodels.optics import split_state_key
from obcom.optics import OpticalGraph, label

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

    async def apply(self, moves: dict[str, str]) -> None:
        for key, symbol in moves.items():
            await self._command(key, symbol)
        await self._prove(moves)

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
        else:
            raise ActuationError(f'{what}: no actuator for kind {spec.kind!r}')
        device.request_special_permission = True
        self._log.info(f'{what} ({detail})')
        try:
            await put()
        except Exception as e:
            raise ActuationError(f'{what}: TIC refused the move ({type(e).__name__}: {e})') from e

    async def _prove(self, moves: dict[str, str]) -> None:
        deadline = time.monotonic() + self._prove_timeout
        version = self._reader.version
        while True:
            selectors = self._reader.snapshot().state.selectors
            pending = {k: v for k, v in moves.items()
                       if k not in selectors or selectors[k].moving or selectors[k].position != v}
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reads = ', '.join(f'{label(self._graph, k)} reads {selectors[k].position if k in selectors else "nothing"}'
                                  f'{" (moving)" if k in selectors and selectors[k].moving else ""}, wanted {v}'
                                  for k, v in pending.items())
                raise ActuationError(f'not proven within {self._prove_timeout:.0f} s: {reads}')
            version = await self._reader.changed(version, remaining)
