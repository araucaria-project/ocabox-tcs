"""Moving selectors through TIC — by component *kind* — and proving that they moved.

Railway doctrine: *commanded ≠ proven*. A move is done only when a fresh reading shows the selector
at the commanded position and no longer moving; until then the operation is in progress, and a
selector that does not get there in ``prove_timeout`` fails the operation with what it reads.

The service moves with its own TIC connection and ``request_special_permission``: the human's
permission was checked by TIC's blocker when they asked for the path. Safety cutoff still applies.
"""
from __future__ import annotations

import asyncio
import logging
import time

from datamodels.optics import split_state_key
from obcom.optics import OpticalGraph

from ocabox_tcs.services.optics.readings import TERTIARY_KINDS, TelescopeReader


class ActuationError(RuntimeError):
    """A move could not be commanded or was not proven."""


class Actuator:
    """Commands selector moves on one telescope and proves them with ``reader``."""

    def __init__(self, telescope, graph: OpticalGraph, reader: TelescopeReader, prove_timeout: float = 60.0,
                 poll: float = 1.0, logger: logging.Logger | None = None):
        self._tel, self._graph, self._reader = telescope, graph, reader
        self._prove_timeout, self._poll = prove_timeout, poll
        self._log = logger or logging.getLogger('optics')

    async def apply(self, moves: dict[str, str]) -> None:
        for key, symbol in moves.items():
            await self._command(key, symbol)
        await self._prove(moves)

    async def _command(self, key: str, symbol: str) -> None:
        component, aspect = split_state_key(key)
        kind = self._graph.node(component).spec.kind
        if kind in TERTIARY_KINDS and aspect is None:
            port = self._graph.node(component).spec.positions[symbol].port
            tertiary = self._tel.get_tertiary(component)
            tertiary.request_special_permission = True
            self._log.info(f'{component} → {symbol} (nasmyth port {port})')
            try:
                await tertiary.aput_selectnasmythport(port)
            except Exception as e:
                raise ActuationError(f'{component} → {symbol}: TIC refused the move ({type(e).__name__}: {e})') from e
            return
        raise ActuationError(f'{key}: no actuator for kind {kind!r}')

    async def _prove(self, moves: dict[str, str]) -> None:
        deadline = time.monotonic() + self._prove_timeout
        while True:
            selectors = (await self._reader.read()).state.selectors
            pending = {k: v for k, v in moves.items()
                       if k not in selectors or selectors[k].moving or selectors[k].position != v}
            if not pending:
                return
            if time.monotonic() >= deadline:
                reads = ', '.join(f'{k} reads {selectors[k].position if k in selectors else "nothing"}'
                                  f'{" (moving)" if k in selectors and selectors[k].moving else ""}, wanted {v}'
                                  for k, v in pending.items())
                raise ActuationError(f'not proven within {self._prove_timeout:.0f} s: {reads}')
            await asyncio.sleep(self._poll)
