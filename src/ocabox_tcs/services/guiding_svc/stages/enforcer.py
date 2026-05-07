"""Enforcer stage — apply corrections via mount pulse-guide.

When pipeline.mode == ``guiding`` and a mount handle + model are wired,
each Correction is converted to (t_N_ms, t_E_ms) by the pulse-guide
model, damped + saturated, and issued via ``mount.aput_pulseguide``.

In ``monitoring`` mode, or when mount/model are absent, the Enforcer
logs would-be pulses without sending them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from auto_adjust.stability import DampingGuard, SaturationGuard
from ocabox_tcs.services.guiding_svc.correction import Correction
from ocabox_tcs.services.guiding_svc.state import Mode, PipelineStateHolder


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


# ASCOM PulseGuide direction codes.
DIR_N = 0
DIR_S = 1
DIR_E = 2
DIR_W = 3


class Enforcer:
    """Apply corrections to the mount.

    Args:
        in_queue: Bounded asyncio.Queue of Correction (from Solver).
        state: Shared pipeline state holder.
        mount: Optional ``ocaboxapi.Mount`` handle. None → log-only.
        pulse_guide_model: Optional ``AdaptiveTransform`` whose ``predict``
            maps ``(dx_px, dy_px)`` → ``(t_N_ms, t_E_ms)``. None → log-only
            even when mount is present (no model to translate with).
        damping: DampingGuard applied to the predicted pulses.
        saturation_ms: SaturationGuard for pulse durations (per axis,
            symmetric ``±max``).
        min_pulse_ms: Below this, the axis is skipped entirely (mount
            doesn't reliably move on micro-pulses).
    """

    def __init__(
        self,
        in_queue: asyncio.Queue[Correction],
        state: PipelineStateHolder,
        mount: Any | None = None,
        *,
        pulse_guide_model: Any | None = None,
        damping: DampingGuard | None = None,
        saturation_ms: SaturationGuard | None = None,
        min_pulse_ms: float = 20.0,
    ) -> None:
        self.in_queue = in_queue
        self.state = state
        self.mount = mount
        self.pulse_guide_model = pulse_guide_model
        self.damping = damping or DampingGuard(alpha_min=0.5, alpha_max=0.5)
        self.saturation_ms = saturation_ms or SaturationGuard(lo=-1500.0, hi=1500.0)
        self.min_pulse_ms = float(min_pulse_ms)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="enforcer")
        logger.debug(
            "Enforcer started (mount=%s model=%s)",
            "live" if self.mount else "<log-only>",
            self.pulse_guide_model.name if self.pulse_guide_model else "<none>",
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                correction = await self.in_queue.get()
            except asyncio.CancelledError:
                break

            snapshot = self.state.snapshot()
            if snapshot.mode != Mode.GUIDING:
                logger.debug(
                    "Enforcer: mode=%s, observing correction (dx=%.2f, dy=%.2f)",
                    snapshot.mode.value,
                    correction.dx_px,
                    correction.dy_px,
                )
                continue

            await self._apply(correction)

    async def _apply(self, correction: Correction) -> None:
        """Translate a Correction into pulse-guide commands and issue them."""
        # 1. Translate (dx_px, dy_px) → (t_N_ms, t_E_ms) via the model.
        if self.pulse_guide_model is None:
            logger.info(
                "Enforcer (no model): would correct dx=%.3f dy=%.3f px",
                correction.dx_px, correction.dy_px,
            )
            return

        prediction = self.pulse_guide_model.predict((correction.dx_px, correction.dy_px))
        t_N_ms, t_E_ms = prediction.y

        # 2. Damp.
        t_N_ms = self.damping.apply(t_N_ms)
        t_E_ms = self.damping.apply(t_E_ms)

        # 3. Saturate (clip).
        t_N_ms, n_clipped = self.saturation_ms.apply_clipped(t_N_ms)
        t_E_ms, e_clipped = self.saturation_ms.apply_clipped(t_E_ms)

        # 4. Direction codes from sign; magnitude stays positive on the wire.
        n_dir, n_dur = (DIR_N, t_N_ms) if t_N_ms >= 0 else (DIR_S, -t_N_ms)
        e_dir, e_dur = (DIR_E, t_E_ms) if t_E_ms >= 0 else (DIR_W, -t_E_ms)

        # 5. Skip below min_pulse_ms (mount can't track such tiny moves).
        n_skip = n_dur < self.min_pulse_ms
        e_skip = e_dur < self.min_pulse_ms

        if self.mount is None:
            logger.info(
                "Enforcer (log-only): dx=%.3f dy=%.3f → "
                "N/S dir=%d dur=%.1fms%s, E/W dir=%d dur=%.1fms%s",
                correction.dx_px, correction.dy_px,
                n_dir, n_dur, " (skip<min)" if n_skip else "",
                e_dir, e_dur, " (skip<min)" if e_skip else "",
            )
            return

        if n_clipped or e_clipped:
            logger.warning(
                "Enforcer: pulse saturation clipped (N=%.1f%s E=%.1f%s)",
                n_dur, "*" if n_clipped else "",
                e_dur, "*" if e_clipped else "",
            )

        # 6. Issue the pulses (sequentially — the ASCOM API takes one
        # axis per call; ocabox/Alpaca handle on-the-wire serialisation).
        # TIC pulseguide handler rejects float Duration with HTTP 400.
        if not n_skip:
            await self.mount.aput_pulseguide(direction=n_dir, duration=int(round(n_dur)))
        if not e_skip:
            await self.mount.aput_pulseguide(direction=e_dir, duration=int(round(e_dur)))

        logger.debug(
            "Enforcer applied: N/S dir=%d dur=%.1fms%s, E/W dir=%d dur=%.1fms%s",
            n_dir, n_dur, " (skipped)" if n_skip else "",
            e_dir, e_dur, " (skipped)" if e_skip else "",
        )
