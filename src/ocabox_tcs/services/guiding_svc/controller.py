"""Controller — authoritative PipelineState mutator.

The Controller is the **only** legitimate writer of PipelineState. All
mutations (operator commands, Solver auto-* policies) flow through it,
so we get atomicity, validation, and arbitration in one place.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from serverish.base import dt_utcnow_array

from ocabox_tcs.services.guiding_svc.pipeline import Pipeline
from ocabox_tcs.services.guiding_svc.state import FramePhase, Mode


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
        # Operator override semantics: when the operator changes the
        # baseline ``exp_time``, drop any auto-exposure override so the
        # operator value takes effect immediately. Auto-exposure that
        # explicitly sets ``current_exp_time`` in the same patch wins.
        if "exp_time" in coerced and "current_exp_time" not in coerced:
            coerced["current_exp_time"] = None
        # Exp_time change invalidates ADU baselines. The narrow-loop's
        # ADU-tolerance gate compares fresh detections against
        # ``acquired_adu``, and the wide-loop scoring uses
        # ``last_acquired_adu`` as a brightness prior. Both samples
        # were captured at the old exposure scale, so a 2× exp_time
        # change moves every fresh star's ADU outside the stale band
        # — the symptom is a continuous "no candidate matches ADU
        # tolerance" cascade until mode is recycled. Clear both
        # baselines on exp_time patch (unless the caller explicitly
        # provides them in the same patch — operator re-locks own
        # value wins). Next acquire refills them at the new scale.
        if "exp_time" in coerced:
            coerced.setdefault("acquired_adu", None)
            coerced.setdefault("last_acquired_adu", None)
        prev_snap = self.pipeline.state.snapshot()
        prev_mode = prev_snap.mode
        prev_method = prev_snap.method
        # Guide-anchor lifecycle: ``guide_anchor`` is the drift
        # reference used by the solver to compute correction.dx/dy in
        # both MONITORING and GUIDING modes. Without it, monitoring
        # falls back to ``central_point`` (the reticle) and "drift"
        # is measured from where the operator's *target* is, not
        # from where the star actually is — confusing on screen
        # (chart looks fine because it has its own snapshot logic,
        # but the textual "last Δ" readout doesn't match).
        #
        # Semantics:
        # - mode → GUIDING: snapshot current acquired_pos (operator
        #   said "hold the star where it is right now").
        # - mode → MONITORING: same as GUIDING — snapshot if we have
        #   a lock; otherwise wait for first acquire to do it (see
        #   ``notify_acquired``).
        # - mode → OFF: clear (session boundary).
        # - explicit caller patch (e.g. drop_to_reticle): wins.
        if "mode" in coerced and "guide_anchor" not in coerced:
            new_mode = coerced["mode"]
            if new_mode in (Mode.GUIDING, Mode.MONITORING) \
                    and prev_mode not in (Mode.GUIDING, Mode.MONITORING):
                if prev_snap.acquired and prev_snap.acquired_pos is not None:
                    coerced["guide_anchor"] = tuple(prev_snap.acquired_pos)
            elif new_mode == Mode.OFF and prev_mode != Mode.OFF:
                coerced["guide_anchor"] = None
        # Mode → OFF is a session boundary — drop the wide-search
        # fingerprint so the next time we light up, we don't try to
        # find "the same star as last night".
        if "mode" in coerced and coerced["mode"] == Mode.OFF and prev_mode != Mode.OFF:
            coerced.setdefault("last_acquired_pos", None)
            coerced.setdefault("last_acquired_adu", None)
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
            new_mode_obj = Mode(coerced["mode"])
            # Pause / resume camera fetching when leaving / entering OFF.
            # Pipeline owns the subscription; controller just signals.
            self.pipeline.apply_mode(new_mode_obj)
            await self._publish_event(
                "mode_changed",
                {"from": prev_mode.value, "to": new_mode_obj.value},
            )
        # Method switch — only take action when the *method name* actually
        # changed; ``method_params`` re-publishes touch the field but
        # don't warrant an instance swap. Pipeline.swap_method
        # instantiates the new method from the registry and hot-swaps it
        # on the live Solver; the operator's ``set_state({"method":...})``
        # therefore takes effect on the very next frame.
        if "method" in coerced and coerced["method"] != prev_method:
            try:
                self.pipeline.swap_method(
                    coerced["method"],
                    coerced.get(
                        "method_params",
                        self.pipeline.state.snapshot().method_params,
                    ),
                )
                await self._publish_event(
                    "method_changed",
                    {"from": prev_method, "to": coerced["method"]},
                )
                await self._publish_journal(
                    f"method changed: {prev_method} → {coerced['method']}"
                )
            except ValueError as e:
                logger.warning("method swap failed: %s", e)
                await self._publish_journal(
                    f"method swap failed: {e}", level="warning"
                )
        return new_state

    async def set_mode(self, mode: Mode | str) -> dict[str, Any]:
        return await self.set_state({"mode": mode})

    async def acquire(self) -> dict[str, Any]:
        """Force the next frame into wide-search mode. Clears the
        last-known star fingerprint so wide search picks closest to
        ``central_point`` rather than dragging us back to whatever
        star was previously held — operator's "fresh start" intent."""
        result = await self.set_state({
            "acquired": False,
            "acquired_pos": None,
            "last_acquired_pos": None,
            "last_acquired_adu": None,
        })
        await self._publish_event("acquire_requested", {})
        return result

    async def acquire_at(self, x: float, y: float) -> dict[str, Any]:
        """Move the operator's reticle (``central_point``) to ``(x, y)``.

        Behaviour depends on mode — the same RPC has two semantics
        because right-click in the UI maps here:

        - **monitoring / off**: full reset semantics — clear ``acquired``
          so the next frame runs wide-search around the new target.
          Last-known fingerprint cleared too. Operator's "look around"
          mode: a right-click teleports the target and starts a fresh
          search.

        - **guiding**: visual-only move. ``central_point`` is updated
          but ``acquired`` and ``guide_anchor`` are left alone — the
          controller continues holding the current lock. This is the
          fix for "right-click during guiding nukes the lock and tries
          to drag a different star to the old anchor": operator's
          intent when dragging the reticle mid-guiding is to re-aim
          the *visual* target (e.g. position the reticle over the
          fibre entrance), not to abandon the held star. Committing
          the new reticle as the actual hold target requires an
          explicit ``drop_to_reticle`` after.
        """
        try:
            xv = float(x)
            yv = float(y)
        except (TypeError, ValueError) as e:
            raise ValueError(f"x/y must be numeric, got x={x!r} y={y!r}") from e
        snap = self.pipeline.state.snapshot()
        patch: dict[str, Any]
        if snap.mode == Mode.GUIDING:
            patch = {"central_point": (xv, yv)}
        else:
            patch = {
                "central_point": (xv, yv),
                "acquired": False,
                "acquired_pos": None,
                "last_acquired_pos": None,
                "last_acquired_adu": None,
            }
        result = await self.set_state(patch)
        await self._publish_event(
            "acquire_at_requested",
            {"x": xv, "y": yv, "mode": snap.mode.value},
        )
        await self._publish_journal(
            f"acquire_at requested: ({xv:.1f}, {yv:.1f})"
            + (" (visual-only — guiding lock retained)" if snap.mode == Mode.GUIDING else "")
        )
        return result

    async def lock_at(self, x: float, y: float) -> dict[str, Any]:
        """Seed the lock onto a star near sensor pixel ``(x, y)``.

        Sets ``acquired_pos`` to the click coords and ``acquired=True``
        so the next solver iteration runs *narrow* search (in a
        ``search_reg_px`` half-window) around the click — no wide
        search, no ``central_point`` change, no mount motion. The
        operator's click is a hint; the solver refines to the actual
        star peak in the box on the next frame.

        ``acquired_adu`` is cleared to ``None`` so the narrow search's
        ADU-tolerance filter doesn't reject the new candidate (it
        re-populates from the next iteration's measured peak).

        This is the routine click-on-frame action — left-click in the
        UI. ``acquire_at`` (changes the operator's target reticle) is
        the rare admin op, mapped to right-click.
        """
        try:
            xv = float(x)
            yv = float(y)
        except (TypeError, ValueError) as e:
            raise ValueError(f"x/y must be numeric, got x={x!r} y={y!r}") from e
        # Operator picking a different star (click or TAB-cycle) is a
        # *re-anchor* request, not "drag this star to the old anchor".
        # In guiding mode also move the guide_anchor onto the new
        # selection so the controller starts holding the new star
        # where it is — no spurious large-error pulses pulling the
        # newly-selected star toward the previous star's locked
        # position.
        snap = self.pipeline.state.snapshot()
        patch: dict[str, Any] = {
            "acquired": True,
            "acquired_pos": (xv, yv),
            "acquired_adu": None,
            # Operator picked a fresh seed — invalidate the smart-sort
            # fingerprint of the previous lock. ADU repopulates from
            # the next narrow detection; meanwhile wide-search smart
            # sort falls back to proximity (= falls back to
            # ``last_acquired_pos`` only).
            "last_acquired_pos": (xv, yv),
            "last_acquired_adu": None,
            # Abandon any pulse plan tied to the previous lock. Without
            # clearing, the next solver iteration would still see
            # ``predicted_pos`` from a pulse fired BEFORE the click
            # and bracket-search from the new ``acquired_pos`` to that
            # stale predicted — wrong region, no detection, lock
            # ping-pongs. Operator click is the explicit "forget what
            # we were doing" signal.
            "predicted_pos": None,
            "active_pulse": None,
            # ``lock_at`` chooses *which star* to track, not *where*
            # to drag it. The click is a seed for narrow search; the
            # actual anchor is the refined centroid the next frame
            # produces. Clearing here lets the bootstrap rule in
            # ``notify_acquired`` set anchor = found-centroid on the
            # very first post-click detection. Operator who wants a
            # specific target uses ``acquire_at`` (reticle move) +
            # ``drop_to_reticle``, not lock_at.
            "guide_anchor": None,
        }
        result = await self.set_state(patch)
        await self._publish_event(
            "lock_at_requested", {"x": xv, "y": yv}
        )
        await self._publish_journal(
            f"lock_at requested: ({xv:.1f}, {yv:.1f}) — anchor will follow refined centroid"
        )
        return result

    async def drop_to_reticle(self) -> dict[str, Any]:
        """Re-anchor active guidance onto the operator's reticle —
        the "drop the star into the fibre" operation.

        Sets ``guide_anchor = central_point``. From the next solver
        iteration the controller pulls the star toward the reticle
        (positioned by the operator over the spectrograph fibre
        entrance, typically via right-click) instead of wherever the
        lock happened to be at mode→guiding transition.

        Pre-condition: ``mode == guiding`` and ``acquired == True``.
        Outside guiding ``guide_anchor`` is unused, so the request is
        rejected with a clear message rather than silently storing a
        value that will be cleared on the next mode flip.

        This is the MVP path for fibre-injection. A fuller Mode-B with
        predict→measure→update narrow-search and auto-promote-after-slew
        is parked.
        """
        snap = self.pipeline.state.snapshot()
        if snap.mode != Mode.GUIDING:
            return {
                "status": "error",
                "error": (
                    f"drop_to_reticle requires mode=guiding "
                    f"(active={snap.mode.value!r}); switch to guiding first."
                ),
            }
        if not snap.acquired or snap.acquired_pos is None:
            return {
                "status": "error",
                "error": "drop_to_reticle requires an active lock — re-acquire first.",
            }
        if snap.central_point is None:
            return {
                "status": "error",
                "error": "drop_to_reticle requires central_point to be set.",
            }
        new_anchor = (float(snap.central_point[0]), float(snap.central_point[1]))
        # Clear any in-flight pulse plan from the prior anchor — drop
        # is a fresh navigation order, the next enforcer iteration
        # produces its own active_pulse against the new target.
        # Without this, a still-set ``predicted_pos`` from a pre-drop
        # pulse would make the solver bracket-search the wrong region.
        await self.pipeline.state.update(
            guide_anchor=new_anchor,
            predicted_pos=None,
            active_pulse=None,
        )
        await self._publish_state(self._snapshot_dict())
        await self._publish_event(
            "drop_to_reticle",
            {"guide_anchor": list(new_anchor), "from_pos": list(snap.acquired_pos)},
        )
        await self._publish_journal(
            f"drop_to_reticle → guide_anchor=({new_anchor[0]:.1f}, {new_anchor[1]:.1f})"
        )
        return {"status": "ok", "guide_anchor": list(new_anchor)}

    async def safety_demote(self, reason: str) -> None:
        """Autonomous safety action — drop ``guiding`` to ``monitoring``.

        Called by the Enforcer's safety guards (repetition guard,
        pulse-failure latch — see ``Enforcer`` docstring) when continuing
        to pulse would harm the observation. ``monitoring`` (not ``off``)
        is the right landing spot: frames keep flowing so the operator
        sees the field and the journal entry explaining what happened,
        but the mount is no longer touched.

        Idempotent and race-tolerant: a no-op unless the pipeline is
        currently in ``guiding``.
        """
        snap = self.pipeline.state.snapshot()
        if snap.mode != Mode.GUIDING:
            return
        logger.error(
            "Controller(%s) SAFETY DEMOTE guiding → monitoring: %s",
            self.pipeline.pipeline_id, reason,
        )
        await self.set_mode(Mode.MONITORING)
        await self._publish_event("safety_demote", {"reason": reason})
        await self._publish_journal(
            f"⚠ SAFETY: guiding demoted to monitoring — {reason}",
            level="error",
        )

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

    async def pulse_pixels(self, *, dx_px: float, dy_px: float) -> dict[str, Any]:
        """Issue a pulse to move the star by an *image-axis pixel*
        target — UI's pixel-mode arrows.

        Solves the per-camera transpose ambiguity: instead of asking
        the operator to remember "N moves star right because of
        ``protocol.transpose: true``", they say "move 30 px in +X" and
        the controller does the Jacobian inversion. Same primitive the
        regular guiding loop uses (``pulse_guide_model.predict``), but
        triggered by an explicit operator command rather than a
        per-frame correction.

        Cap: the same ``saturation_ms`` the Enforcer respects for
        guiding pulses applies — clamps each axis to
        ``duration_max_ms``. Reported back so the UI can show the
        operator how much of their request actually happened.
        """
        if self.pipeline._enforcer is None or self.pipeline._enforcer.pulse_guide_model is None:
            return {"status": "error", "error": "no pulse-guide model loaded"}
        try:
            dx = float(dx_px)
            dy = float(dy_px)
        except (TypeError, ValueError) as e:
            raise ValueError(f"dx_px/dy_px must be numeric, got dx={dx_px!r} dy={dy_px!r}") from e
        model = self.pipeline._enforcer.pulse_guide_model
        # ``predict()`` is the *cancel-error* primitive used by the
        # guiding loop: input = "the star is dx,dy off target", output
        # = pulses that move it by (-dx,-dy) to bring it back. For the
        # operator's "move the star by +30 px" intent we need the
        # *forward* sense — feed -(dx,dy) to get pulses that move +
        # (dx,dy). One sign flip; semantics consistent with the rest
        # of the codebase.
        prediction = model.predict((-dx, -dy))
        t_N_ms, t_E_ms = prediction.y
        cap_ms = float(self.pipeline._enforcer.saturation_ms.hi)
        # Clip each axis independently so the operator gets two
        # truthful pulses, not one weirdly-scaled compromise.
        clip_n = abs(t_N_ms) > cap_ms
        clip_e = abs(t_E_ms) > cap_ms
        t_N_ms = max(-cap_ms, min(cap_ms, t_N_ms))
        t_E_ms = max(-cap_ms, min(cap_ms, t_E_ms))
        # Translate signed durations into ASCOM (direction, abs_dur) pairs.
        n_dir, n_dur = (0, t_N_ms) if t_N_ms >= 0 else (1, -t_N_ms)
        e_dir, e_dur = (2, t_E_ms) if t_E_ms >= 0 else (3, -t_E_ms)
        results: list[dict[str, Any]] = []
        for direction, dur in ((n_dir, n_dur), (e_dir, e_dur)):
            if dur < 1.0:  # below quantisation; skip
                continue
            r = await self.manual_pulse(direction=direction, duration_ms=dur)
            results.append(r)
        return {
            "status": "ok",
            "requested_dx_px": dx,
            "requested_dy_px": dy,
            "pulses": results,
            "n_clipped": clip_n,
            "e_clipped": clip_e,
        }

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

        # A manual nudge invalidates the current "where to hold" target —
        # the operator has explicitly moved the mount, so the previous
        # anchor (and any in-flight auto-pulse plan from the Enforcer)
        # no longer matches their intent. Clear all three; the bootstrap
        # rule in ``notify_acquired`` will set a fresh anchor on the
        # next post-pulse re-acquire (= where the star ends up).
        await self.pipeline.state.update(
            guide_anchor=None,
            predicted_pos=None,
            active_pulse=None,
        )

        # TIC pulseguide handler rejects float Duration with HTTP 400.
        # 5 s deadline matches Enforcer's autopulse path — well over a
        # healthy round-trip but bounded against a stuck mount handler.
        try:
            await asyncio.wait_for(
                mount.aput_pulseguide(direction=dir_code, duration=int(round(duration))),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("manual pulse_guide aput timed out after 5 s")
            return {"status": "error", "error": "aput_pulseguide timeout (5 s)"}
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
    # Calibration probe — measure (dx, dy) of star motion per pulse-ms
    # ------------------------------------------------------------------

    # Number of post-pulse frames to wait before reading the new
    # acquired_pos. >1 lets the lock settle on the post-pulse star
    # position before we sample.
    _CAL_SETTLE_FRAMES = 3

    # Default extra time after pulse-end before we accept a measurement,
    # to absorb camera readout + any residual mount mechanical settle.
    # 2.5 s was empirically calibrated on jk15 BESO (2026-05-07): below
    # this the mount is still drifting from the pulse and individual
    # probe σ blows up to ~5-10 px. At 2.5 s σ drops to ~2-4 px, which
    # is dominated by detection noise + sidereal sub-pixel jitter rather
    # than mechanical residual. Tuneable via the RPC parameter for
    # mounts with different settle profiles.
    _CAL_POST_PULSE_SETTLE_MS = 2500.0

    async def calibrate_probe(
        self,
        *,
        direction: int | str,
        duration_ms: float,
        settle_frames: int | None = None,
        post_pulse_settle_ms: float | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        """One-shot calibration probe — measure star displacement
        caused by a known pulse.

        Pre-conditions: ``acquired=True`` (need a star to track) AND
        ``mode=monitoring`` (otherwise the guider's own corrections
        compete with the probe and you measure nonsense). The RPC
        refuses if either is violated rather than silently giving bad
        data.

        Workflow:
          1. Sample ``acquired_pos`` and ``state.version``.
          2. Issue a manual pulse via the mount (``aput_pulseguide`` is
             fire-and-forget — the call returns immediately, the mount
             keeps moving the star for ``duration_ms``).
          3. **Wait for the pulse to physically complete** — ``duration_ms``
             of mount motion plus a configurable settle margin so the
             frame we sample was captured *after* all star motion has
             stopped. Without this we'd sample mid-pulse and the
             measured ``(dx, dy)`` would be a fraction of the true effect,
             with ongoing sidereal drift smeared on top — biasing the
             whole calibrated Jacobian.
          4. Then wait for at least one fresh state version bump after
             the settle deadline so the sampled ``acquired_pos`` is from
             a frame entirely captured post-pulse.
          5. Sample new ``acquired_pos``; return the delta + per-ms rates.

        Returns ``{"status": "ok", "dx", "dy", "k_x_per_ms", "k_y_per_ms",
                  "pos_before", "pos_after", "duration_ms", "direction_label"}``
        on success, or ``{"status": "error", "error": "…"}`` on validation
        / timeout failure.
        """
        snap = self.pipeline.state.snapshot()
        if not snap.acquired or snap.acquired_pos is None:
            return {"status": "error", "error": "not acquired — lock a star first"}
        if snap.mode != Mode.MONITORING:
            return {
                "status": "error",
                "error": (
                    f"need mode=monitoring (active={snap.mode.value!r}); "
                    "switch to monitoring so guiding corrections don't "
                    "compete with the probe"
                ),
            }

        settle_ms = (
            float(post_pulse_settle_ms)
            if post_pulse_settle_ms is not None else self._CAL_POST_PULSE_SETTLE_MS
        )
        pos_before = tuple(snap.acquired_pos)

        # Issue the pulse — manual_pulse handles direction validation,
        # cap, journal/event publishing, and the no-mount path.
        pulse_result = await self.manual_pulse(
            direction=direction, duration_ms=duration_ms
        )
        if pulse_result.get("status") != "ok":
            return {
                "status": "error",
                "error": f"pulse failed: {pulse_result.get('status')}",
                "pulse_result": pulse_result,
            }
        dir_label = pulse_result["direction_label"]

        # Phase A: wait for the pulse to physically complete + settle.
        # ``aput_pulseguide`` is fire-and-forget; only after this elapsed
        # time can we trust the star to be at its post-pulse position.
        wait_total_s = float(duration_ms) / 1000.0 + settle_ms / 1000.0
        await asyncio.sleep(wait_total_s)

        # Phase B: wait for a fresh frame post-settle that *also* has
        # the lock recovered. During a long pulse the pipeline's narrow
        # search can transiently lose lock; wide-search recovery may
        # take 1-2 frames depending on star density and how far the
        # pulse pushed the star. So we don't abort on any one
        # acquired=False snapshot — we keep polling until either:
        #   * a fresh frame (version > version_at_settle) reports
        #     acquired=True with a position → success, or
        #   * the timeout elapses → genuinely lost.
        # This is the correct behaviour for the *operator's* notion of
        # "did the probe succeed": if the pipeline can re-establish the
        # lock within a reasonable wait, the post-pulse position is
        # exactly what we want to measure. The earlier "first fresh
        # frame wins" logic occasionally fired a false "lock lost"
        # error during the recovery window, throwing away the rest of
        # the calibration session.
        version_at_settle = self.pipeline.state.snapshot().version
        deadline = asyncio.get_event_loop().time() + float(timeout_s)
        while True:
            await asyncio.sleep(0.1)
            snap = self.pipeline.state.snapshot()
            if (
                snap.version > version_at_settle
                and snap.acquired
                and snap.acquired_pos is not None
            ):
                break
            if asyncio.get_event_loop().time() > deadline:
                if snap.version <= version_at_settle:
                    return {
                        "status": "error",
                        "error": (
                            f"timeout waiting for fresh frame after "
                            f"pulse-settle (version stuck at {snap.version}). "
                            "Pipeline may be stalled or exposure cadence "
                            "is slower than the timeout."
                        ),
                    }
                return {
                    "status": "error",
                    "error": (
                        f"lock not recovered after pulse — pulse={dir_label} "
                        f"{duration_ms:.0f}ms pushed star outside narrow search "
                        "and wide-search did not re-acquire within "
                        f"{timeout_s:.0f}s. Try a shorter probe, or widen "
                        "wide_search_radius_px."
                    ),
                }

        pos_after = tuple(snap.acquired_pos)
        dx = float(pos_after[0] - pos_before[0])
        dy = float(pos_after[1] - pos_before[1])
        dur = float(duration_ms)
        result = {
            "status": "ok",
            "direction_label": dir_label,
            "duration_ms": dur,
            "pos_before": list(pos_before),
            "pos_after": list(pos_after),
            "dx": dx,
            "dy": dy,
            "k_x_per_ms": dx / dur if dur > 0 else 0.0,
            "k_y_per_ms": dy / dur if dur > 0 else 0.0,
        }
        await self._publish_journal(
            f"calibrate_probe {dir_label} {dur:.0f}ms → "
            f"dx={dx:+.2f} dy={dy:+.2f} px "
            f"(k=({result['k_x_per_ms']:+.5f},{result['k_y_per_ms']:+.5f}) px/ms)"
        )
        return result

    # ------------------------------------------------------------------
    # Solver-triggered events (called from inside the pipeline)
    # ------------------------------------------------------------------

    async def notify_acquired(self, *, acquired: bool, position: tuple[float, float] | None,
                              adu: float | None,
                              candidates: list[tuple[float, float, float]] | None = None,
                              recovery: bool = False,
                              frame_phase: str | None = None,
                              ) -> None:
        """Solver tells the Controller a star was (re-)acquired or lost.

        Args:
            acquired: True = solver has a lock this frame; False = no lock.
            position: Sub-pixel ``(x, y)`` of the lock when acquired.
            adu: Peak ADU of the locked star.
            candidates: Per-frame detection list ``[(x, y, adu), …]`` —
                ``None`` leaves the previous list untouched.
            recovery: True when this is a wide-search re-acquisition
                following a confirmed lock-loss (narrow miss budget
                exhausted). When set AND mode=guiding, ``guide_anchor``
                is reset to the new ``position`` — the safe-failure
                rule: after we genuinely lost the star and a *different*
                detection regained it, we don't trust we're still on
                the same physical star, so we stop dragging it toward
                the previous anchor. Operator must explicitly re-issue
                ``drop_to_reticle`` (or accept the new anchor as the
                hold target). False during routine narrow-track frames
                and stick-with-it grace updates — those don't disturb
                the anchor.

        Maintains ``last_acquired_pos`` / ``last_acquired_adu`` —
        persisted across loss so wide-search smart sort can favour the
        same physical star (proximity + ADU similarity) on the next
        recovery. Updated only on successful acquisitions; preserved
        verbatim when ``acquired=False``.
        """
        prev = self.pipeline.state.snapshot()
        update_kwargs: dict[str, Any] = dict(
            acquired=acquired,
            acquired_pos=position,
            acquired_adu=adu,
            acquired_at_ts=dt_utcnow_array() if acquired else prev.acquired_at_ts,
        )
        if frame_phase is not None:
            update_kwargs["frame_phase"] = frame_phase
        # Refresh last-known on every successful detection — that's
        # what wide-search-after-loss reaches for. On loss leave the
        # prior values intact (the field is *the most recent good
        # detection*, not "the current").
        if acquired and position is not None:
            update_kwargs["last_acquired_pos"] = position
            if adu is not None:
                update_kwargs["last_acquired_adu"] = float(adu)
            # Jacobian-fidelity sample. The first ACQUIRING-phase frame
            # after a pulse settles is the only one that gives an
            # unbiased read on jacobian accuracy: the mount has stopped
            # moving, sidereal drift over the settle window is small
            # compared to the commanded motion, and we have both the
            # pre-pulse position (``active_pulse.src_pos``) and the
            # forward-Jacobian prediction (``active_pulse.predicted_pos``)
            # to compare with the freshly measured ``position``. Log
            # once per pulse — operator can scrape these out of the
            # journal and roll up per-axis residual stats without
            # running a dedicated calibration probe.
            if (
                frame_phase == FramePhase.ACQUIRING.value
                and prev.active_pulse is not None
            ):
                ap = prev.active_pulse
                src = getattr(ap, "src_pos", None) if not isinstance(ap, dict) else ap.get("src_pos")
                pred = getattr(ap, "predicted_pos", None) if not isinstance(ap, dict) else ap.get("predicted_pos")
                t_n = getattr(ap, "pulse_t_n_ms", None) if not isinstance(ap, dict) else ap.get("pulse_t_n_ms")
                t_e = getattr(ap, "pulse_t_e_ms", None) if not isinstance(ap, dict) else ap.get("pulse_t_e_ms")
                if src is not None and pred is not None and t_n is not None and t_e is not None:
                    exp_dx = pred[0] - src[0]
                    exp_dy = pred[1] - src[1]
                    act_dx = position[0] - src[0]
                    act_dy = position[1] - src[1]
                    res_dx = act_dx - exp_dx
                    res_dy = act_dy - exp_dy
                    def _pct(num: float, den: float) -> str:
                        return f"{abs(num / den) * 100:.1f}%" if abs(den) > 0.5 else "—"
                    logger.info(
                        "[acquiring] jacobian residual: pulse t_N=%+.0fms t_E=%+.0fms "
                        "src=(%.1f,%.1f) predicted=(%.1f,%.1f) actual=(%.1f,%.1f) "
                        "motion exp=(%+.2f,%+.2f) act=(%+.2f,%+.2f) "
                        "residual=(%+.2f,%+.2f)px (%s X, %s Y)",
                        t_n, t_e,
                        src[0], src[1], pred[0], pred[1], position[0], position[1],
                        exp_dx, exp_dy, act_dx, act_dy, res_dx, res_dy,
                        _pct(res_dx, exp_dx), _pct(res_dy, exp_dy),
                    )
            # Prediction served its purpose — narrow search latched onto
            # the star at (or near) the predicted spot. Clear both the
            # legacy ``predicted_pos`` handle and the first-class
            # ``active_pulse`` record so the pipeline returns to the
            # TRACKING phase. Keeping them in lock-step avoids any
            # consumer reading half-stale state during the Phase 2
            # transition.
            #
            # Don't clear during IN_FLIGHT / SETTLING: in those phases
            # the solver calls ``notify_acquired`` with the previous
            # frame's ``acquired/position`` snapshot purely to keep the
            # UI's last-known marker visible and swap the phase pill —
            # not because we actually re-acquired. Clearing
            # ``active_pulse`` here would prematurely end the trajectory
            # phase before the ACQUIRING frame arrives, which then
            # cannot log the jacobian residual (no source pulse data
            # left to diff against) and the UI loses the predicted-pos
            # arrow mid-flight. Only TRACKING and ACQUIRING are real
            # measurements; only they should clear.
            if frame_phase not in (
                FramePhase.IN_FLIGHT.value,
                FramePhase.SETTLING.value,
            ):
                update_kwargs["predicted_pos"] = None
                update_kwargs["active_pulse"] = None
        # Wide-search recovery anchor logic — distance gate.
        #
        # Smart-sort biases toward the same star (proximity + ADU),
        # but no guarantee. We compare the recovered position against
        # the expected position to decide whether to keep or reset
        # anchor:
        #   - expected = predicted_pos if a pulse was in flight,
        #     else last_acquired_pos (steady-tracking loss).
        #   - if recovered within RECOVERY_SAME_STAR_PX of expected →
        #     same physical star, keep anchor (continue the plan).
        #   - if further → either a different star, or mount went
        #     somewhere we didn't expect; reset anchor to recovered
        #     position so we don't try to drag a random star toward
        #     an old target the operator no longer cares about.
        # During a deliberate drop_to_reticle, this means: if timing
        # is right we never reach this branch (narrow keeps lock through
        # ACQUIRING); if narrow fails and wide brings the same star
        # close to predicted_pos, anchor stays at central_point and
        # slew continues; if wide finds something far, slew aborts
        # and operator notices.
        if recovery and acquired and position is not None:
            expected = prev.predicted_pos or prev.last_acquired_pos
            if expected is not None:
                dx = float(position[0]) - float(expected[0])
                dy = float(position[1]) - float(expected[1])
                search_reg = float(getattr(prev, "search_reg_px", 25) or 25)
                threshold_px = 2.0 * search_reg
                if (dx * dx + dy * dy) > threshold_px * threshold_px:
                    update_kwargs["guide_anchor"] = (float(position[0]), float(position[1]))
                    logger.info(
                        "wide-recovery: found at (%.1f, %.1f), expected (%.1f, %.1f), "
                        "dist > %.1fpx — resetting anchor",
                        position[0], position[1], expected[0], expected[1], threshold_px,
                    )
        # Anchor bootstrap. Whenever we hold a lock but guide_anchor is
        # None, set it to the current acquired_pos. Covers:
        #   - first acquire after mode→MONITORING/GUIDING from OFF (no
        #     prior lock to snapshot from at mode-change time);
        #   - first acquire after lock_at (which deliberately clears
        #     anchor so the refined centroid wins over the click);
        #   - first acquire after manual_pulse (operator nudged, the
        #     new equilibrium IS the target);
        #   - any other path that clears anchor.
        # No-op once anchor is set; subsequent frames in the same
        # session don't re-trigger.
        if (
            acquired
            and position is not None
            and prev.guide_anchor is None
            and prev.mode != Mode.OFF
            and "guide_anchor" not in update_kwargs  # already set above (wide-recovery)
        ):
            update_kwargs["guide_anchor"] = (float(position[0]), float(position[1]))
        if candidates is not None:
            update_kwargs["candidates"] = candidates
        await self.pipeline.state.update(**update_kwargs)
        # Runtime counter bookkeeping — single choke point for every
        # solver cycle outcome, so per-pipeline acquired-ratio metrics
        # don't need separate plumbing through Solver.
        self.pipeline.record_cycle(acquired=bool(acquired))
        await self._publish_state(self._snapshot_dict())
        if acquired and not prev.acquired:
            ev = "acquired_gained"
            payload: dict[str, Any] = {"position": list(position) if position else None}
            if recovery:
                payload["recovery"] = True
                if prev.mode == Mode.GUIDING:
                    payload["anchor_reset"] = True
            await self._publish_event(ev, payload)
            if recovery and prev.mode == Mode.GUIDING:
                await self._publish_journal(
                    f"wide-search recovery — guide_anchor → "
                    f"({position[0]:.1f}, {position[1]:.1f})"
                )
        elif not acquired and prev.acquired:
            await self._publish_event("acquired_lost", {})

    # ------------------------------------------------------------------
    # Internal publish helpers
    # ------------------------------------------------------------------

    def _snapshot_dict(self) -> dict[str, Any]:
        return self.pipeline.state.snapshot().to_dict()

    # NATS publish timeout. Any single publish that takes longer than
    # this is treated as a transient infrastructure issue (broker
    # blip, JetStream backpressure, network glitch). We drop the
    # message and continue rather than block the pipeline. State
    # messages are self-healing — the next state mutation republishes
    # everything; events are journal-style and a missed one is
    # acceptable over a hung solver. Anything > 1 s here is already
    # pathological in a healthy LAN.
    _PUBLISH_TIMEOUT_S = 2.0

    async def _publish_state(self, snapshot_dict: dict[str, Any]) -> None:
        if self.state_publisher is None:
            return
        try:
            await asyncio.wait_for(
                self.state_publisher.publish(
                    data=snapshot_dict,
                    meta={"message_type": "default", "sender": self.sender_id},
                ),
                timeout=self._PUBLISH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "state publish timed out after %.1fs — message dropped, "
                "next mutation will republish", self._PUBLISH_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("state publish failed: %s", e)

    async def _publish_event(self, event: str, payload: dict[str, Any]) -> None:
        if self.events_publisher is None:
            return
        try:
            await asyncio.wait_for(
                self.events_publisher.publish(
                    data={"event": event, "payload": payload, "ts": dt_utcnow_array()},
                    meta={"message_type": "default", "sender": self.sender_id},
                ),
                timeout=self._PUBLISH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "event publish timed out after %.1fs (event=%s) — dropped",
                self._PUBLISH_TIMEOUT_S, event,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("event publish failed: %s", e)

    async def _publish_journal(self, message: str, level: str = "info") -> None:
        if self.journal_publisher is None:
            logger.info("[journal/%s] %s", self.sender_id, message)
            return
        try:
            # MsgJournalPublisher exposes a logging-like interface that
            # fills in timestamp/conversation_id/op to satisfy the
            # journal schema.
            method = getattr(self.journal_publisher, level, self.journal_publisher.info)
            await asyncio.wait_for(method(message), timeout=self._PUBLISH_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "journal publish timed out after %.1fs — message: %r",
                self._PUBLISH_TIMEOUT_S, message,
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
