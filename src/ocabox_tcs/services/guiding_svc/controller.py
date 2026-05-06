"""Controller — authoritative PipelineState mutator.

The Controller is the **only** legitimate writer of PipelineState. All
mutations (operator commands, Solver auto-* policies) flow through it,
so we get atomicity, validation, and arbitration in one place.

"""

from __future__ import annotations

import logging
from typing import Any

from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.state import Mode


logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[-1])


class Controller:
    """Authoritative state mutator for one Pipeline.

    Method surface (also the RPC vocabulary):
      - set_state / set_mode / acquire — operator commands
      - snapshot — read-only state dump
      - dark_rebuild / bias_rebuild — calibration triggers (stubs)
      - request_auto_state — auto-policy updates from Solver
      - notify_acquired — solver-driven lock-state change

    Per-camera arbitration ("at most one pipeline in guiding") is the
    Manager's job; Controller proposes, Manager approves.
    """

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        # Optional: callbacks injected by the Manager for arbitration.
        # Frame iteration: we don't enforce arbitration yet.
        self.on_mode_change_request = None
        # NATS publishers wired by Manager during init (stage 1A).
        self.state_publisher: Any | None = None
        self.events_publisher: Any | None = None
        self.journal_publisher: Any | None = None
        # Sender identity for outgoing message envelopes.
        self.sender_id: str = pipeline.pipeline_id

    # ------------------------------------------------------------------
    # State mutation
    # ------------------------------------------------------------------

    async def set_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial state update. Returns the new state snapshot."""
        if not patch:
            return self._snapshot_dict()
        coerced = _coerce_patch(patch)
        prev_mode = self.pipeline.state.snapshot().mode
        version = await self.pipeline.state.update(**coerced)
        logger.info(
            "Controller(%s).set_state version=%d patch_keys=%s",
            self.pipeline.pipeline_id,
            version,
            list(coerced),
        )
        new_state = self._snapshot_dict()
        await self._publish_state(new_state)
        if "mode" in coerced and coerced["mode"] != prev_mode:
            await self._publish_event(
                "mode_changed",
                {"from": prev_mode.value, "to": Mode(coerced["mode"]).value},
            )
        return new_state

    async def set_mode(self, mode: Mode | str) -> dict[str, Any]:
        return await self.set_state({"mode": mode})

    async def acquire(self) -> dict[str, Any]:
        """Force the next frame into wide-search mode."""
        result = await self.set_state({"acquired": False, "acquired_pos": None})
        await self._publish_event("acquire_requested", {})
        return result

    async def request_auto_state(self, **suggested: Any) -> dict[str, Any]:
        """Apply an auto-* policy state change requested by Solver.

        Stage 1A: identical to set_state. Real implementation will
        validate that the request is from a legitimate auto-policy
        source (not bypassing operator).
        """
        return await self.set_state(suggested)

    # ------------------------------------------------------------------
    # Read-only / external triggers (RPC surface)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return the current PipelineState as a JSON-friendly dict."""
        return self._snapshot_dict()

    async def dark_rebuild(self, **params: Any) -> dict[str, Any]:
        """Trigger a master-dark rebuild. Stub until Phase 4 calibration.

        Acks the request via journal/events so operator UIs see something
        happen; the actual build pipeline lands with the calibration work.
        """
        await self._publish_journal(
            f"dark_rebuild requested (params={params}); not implemented yet"
        )
        return {
            "status": "queued",
            "implemented": False,
            "phase": "Phase 4",
            "params": params,
        }

    async def bias_rebuild(self, **params: Any) -> dict[str, Any]:
        """Trigger a master-bias rebuild. Stub — see dark_rebuild."""
        await self._publish_journal(
            f"bias_rebuild requested (params={params}); not implemented yet"
        )
        return {
            "status": "queued",
            "implemented": False,
            "phase": "Phase 4",
            "params": params,
        }

    # ASCOM PulseGuide direction codes accepted by `manual_pulse`.
    _PULSE_DIRECTIONS = {0, 1, 2, 3}
    # Hard cap on a single manual pulse — protects against finger-fumbles.
    _MANUAL_PULSE_MAX_MS = 5000.0

    async def manual_pulse(
        self,
        *,
        direction: int | str,
        duration_ms: float,
    ) -> dict[str, Any]:
        """Issue a single pulse-guide command directly, bypassing the
        Solver/Enforcer chain.

        Used by the operator (or UI) for hand-calibration probes and
        emergency nudges. Goes around the damping/saturation guards: the
        caller specified the duration and we honour it (subject to a hard
        cap of ``_MANUAL_PULSE_MAX_MS``).

        Direction may be the ASCOM integer code (0=N, 1=S, 2=E, 3=W) or
        the corresponding letter. ``duration_ms`` is in milliseconds.

        When the mount handle isn't wired (sim/dev), the call is logged
        and returns ``status: no_mount``.
        """
        dir_code = _coerce_direction(direction)
        if dir_code not in self._PULSE_DIRECTIONS:
            raise ValueError(
                f"direction must be 0..3 (N/S/E/W), got {direction!r}"
            )
        try:
            duration = float(duration_ms)
        except (TypeError, ValueError) as e:
            raise ValueError(f"duration_ms must be numeric, got {duration_ms!r}") from e
        if duration < 0:
            raise ValueError(f"duration_ms must be ≥ 0, got {duration_ms}")
        if duration > self._MANUAL_PULSE_MAX_MS:
            raise ValueError(
                f"duration_ms {duration} exceeds manual-pulse cap "
                f"{self._MANUAL_PULSE_MAX_MS} (refusing as a safety measure)"
            )

        mount = self.pipeline.mount
        dir_label = _DIRECTION_LABELS[dir_code]

        if mount is None:
            await self._publish_journal(
                f"manual_pulse {dir_label} {duration:.0f}ms — no mount wired (logged only)"
            )
            return {
                "status": "no_mount",
                "direction": dir_code,
                "direction_label": dir_label,
                "duration_ms": duration,
            }

        await mount.aput_pulseguide(direction=dir_code, duration=duration)
        await self._publish_journal(
            f"manual_pulse {dir_label} {duration:.0f}ms"
        )
        await self._publish_event(
            "manual_pulse",
            {"direction": dir_code, "direction_label": dir_label, "duration_ms": duration},
        )
        return {
            "status": "ok",
            "direction": dir_code,
            "direction_label": dir_label,
            "duration_ms": duration,
        }

    # ------------------------------------------------------------------
    # Solver-triggered events (called from inside the pipeline)
    # ------------------------------------------------------------------

    async def notify_acquired(self, *, acquired: bool, position: tuple[float, float] | None,
                              adu: float | None) -> None:
        """Solver tells the Controller a star was (re-)acquired or lost."""
        prev = self.pipeline.state.snapshot()
        await self.pipeline.state.update(
            acquired=acquired,
            acquired_pos=position,
            acquired_adu=adu,
            acquired_at_ts=dt_utcnow_array() if acquired else prev.acquired_at_ts,
        )
        await self._publish_state(self._snapshot_dict())
        if acquired and not prev.acquired:
            await self._publish_event(
                "acquired_gained", {"position": list(position) if position else None}
            )
        elif not acquired and prev.acquired:
            await self._publish_event("acquired_lost", {})

    # ------------------------------------------------------------------
    # Internal publish helpers
    # ------------------------------------------------------------------

    def _snapshot_dict(self) -> dict[str, Any]:
        return self.pipeline.state.snapshot().to_dict()

    async def _publish_state(self, snapshot_dict: dict[str, Any]) -> None:
        if self.state_publisher is None:
            return
        try:
            await self.state_publisher.publish(
                data=snapshot_dict,
                meta={"message_type": "guider.state", "sender": self.sender_id},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("state publish failed: %s", e)

    async def _publish_event(self, event: str, payload: dict[str, Any]) -> None:
        if self.events_publisher is None:
            return
        try:
            await self.events_publisher.publish(
                data={"event": event, "payload": payload, "ts": dt_utcnow_array()},
                meta={"message_type": "guider.event", "sender": self.sender_id},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("event publish failed: %s", e)

    async def _publish_journal(self, message: str, level: str = "info") -> None:
        if self.journal_publisher is None:
            logger.info("[journal/%s] %s", self.sender_id, message)
            return
        try:
            await self.journal_publisher.publish(
                data={"message": message, "level": level},
                meta={"message_type": "journal", "sender": self.sender_id},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("journal publish failed: %s", e)


_DIRECTION_LABELS = {0: "N", 1: "S", 2: "E", 3: "W"}
_DIRECTION_FROM_LETTER = {"N": 0, "S": 1, "E": 2, "W": 3}


def _coerce_direction(direction: int | str) -> int:
    """Map a direction value (int code or single letter) to ASCOM int code."""
    if isinstance(direction, str):
        return _DIRECTION_FROM_LETTER.get(direction.strip().upper(), -1)
    try:
        return int(direction)
    except (TypeError, ValueError):
        return -1


def _coerce_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Coerce wire-format values into the right Python types.

    Currently: `mode` strings → Mode enum. Future: validation, range
    checks, deny-list of read-only fields.
    """
    out = dict(patch)
    if "mode" in out and isinstance(out["mode"], str):
        out["mode"] = Mode(out["mode"])
    # `acquired_at_ts` is a 7-int array; pass through.
    return out
