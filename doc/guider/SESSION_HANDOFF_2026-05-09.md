# Guider session handoff — 2026-05-09

You can resume this work in any Claude Code session (local terminal,
`claude.ai/code` in a browser, or a different machine) by reading this
file. The repo + memory state are sufficient to pick up without me
re-explaining context.

## Current state

- **Production deploy**: `services01.oca.lan` running `oca_guider_jk15.service`
  on branch `feat/guider_svc` HEAD = `c60e0e8`. Update flow in
  `deploy/README.md` (one-liner: `git pull && poetry install --sync &&
  ng build && systemctl restart`).
- **What works**: continuous tracking with small corrections is reliable.
  Thumbnails render. UI auto-discovers `thumbnailHttpBase` and
  `thumbnailPathPrefix` from NATS now.
- **What doesn't work reliably**: drop-to-reticle (and any large `lock_at`
  correction). The star is lost during the slew, narrow-search demotes to
  wide, anchor resets, slew cancelled. Operator's bug report verbatim:
  *"drop to rec nigdy sie nie udaje. jakos timing robienia poprawek -
  prediction gdzie gwiazda ma sie znalezc i tego co robi telesko wypada
  tak ze zawsze sie zgubia po drodze."*

## The diagnosis (operator-confirmed)

The pipeline has no first-class temporal model. "Phase" lives implicitly
across three places:

| Field | File | Means | Fails to model |
|---|---|---|---|
| `_pulse_end_monotonic` | `enforcer.py:88` | "drop corrections until T" | Visible only to enforcer |
| `predicted_pos` | `state.py`, written `enforcer.py:265+` | "after this pulse, star will be here" | No time — solver can't tell mid-flight from settled |
| `narrow_miss_count` | `single_star.py:541` | "tolerate N consecutive misses" | Counts mid-pulse (motion-blurred, expected) misses against the budget |

Frame timestamps and pulse timestamps live on different clocks
(`dt_utcnow_array` vs `time.monotonic()`), so cross-correlation requires
ad-hoc bookkeeping that nobody maintains.

## Proposed architecture (operator-approved direction)

Single source of truth: `PipelineState.active_pulse: PulseEvent | None`.

```python
@dataclass
class PulseEvent:
    issued_utc: list[int]          # serverish UTC array, T0 of pulse
    motion_end_utc: list[int]      # T0 + total active pulse ms
    settled_utc: list[int]         # motion_end + post_pulse_settle_ms
    src_pos: tuple[float, float]   # acquired_pos at issue time
    predicted_pos: tuple[float, float]  # src + forward_J · t_actual
    pulse_t_n_ms: float            # signed, after damping+clip
    pulse_t_e_ms: float
```

Frame phase derivation (in solver, per-frame):

```
phase(frame, active_pulse) =
    None or frame.t_mid < issued_utc            → TRACKING
    issued_utc <= frame.t_mid < motion_end_utc  → IN_FLIGHT
    motion_end_utc <= frame.t_mid < settled_utc → SETTLING
    settled_utc <= frame.t_mid                  → ACQUIRING
```

Solver behavior per phase:
- `TRACKING`: detect, narrow-box around `acquired_pos`, normal miss accounting.
- `IN_FLIGHT` / `SETTLING`: **skip detect**, hold `acquired_pos`,
  publish state with `frame_phase` for GUI.
- `ACQUIRING`: detect, search bracket-box around `predicted_pos` with
  generous margin (e.g. 1.5×`half`); first hit → clear `active_pulse`,
  set new `acquired_pos`, transition to `TRACKING`. After N consecutive
  ACQUIRING failures → wide-search recovery (anchor reset, current
  behavior).

GUI (in `ocabox-guider-ui`):
- `frame_phase=IN_FLIGHT/SETTLING` → render an oval/arrow overlay from
  `src_pos` to `predicted_pos` (the operator sees motion-blur expectation,
  not "lock lost").
- Status pill: TRACKING / PULSING / SETTLING / ACQUIRING (today shows OFF/MONITORING/GUIDING).
- Drift chart: settle-done markers next to existing pulse markers.

## Phased rollout

Each phase is a separate, independently-reviewable commit.

### Phase 1 — clean data model, no behavior change

Touch: `state.py`, `enforcer.py`. Add `PulseEvent` dataclass and the
`active_pulse` field. Enforcer writes a full `PulseEvent` after each
pulse alongside the existing `predicted_pos`, `_pulse_end_monotonic`
(keep them — phase 2 removes the duplication).

Test: deploy, drop-to-reticle, observe `active_pulse` populated in NATS
state messages. Existing behavior unchanged.

### Phase 2 — solver respects phases (THE fix)

Touch: `single_star.py`, `controller.py`. Frame classification helper.
Skip detect on `IN_FLIGHT/SETTLING`. `ACQUIRING` uses bracket box
around `active_pulse.predicted_pos`. On successful acquire in
`ACQUIRING`, clear `active_pulse`. Drop the
`predicted_pos`-as-grace-flag hack (replaced by phase machine).

Test: drop-to-reticle should reliably land the star at reticle within
2-3 pulse cycles. Big `lock_at` corrections should hold lock through
the slew.

### Phase 3 — GUI temporal feedback

Touch: `ocabox-guider-ui/src/services/guider.store.ts` (read
`frame_phase` from state), drift-chart component (settle markers),
guider-image overlay component (oval for IN_FLIGHT/SETTLING).

### Phase 4 — bounded waits everywhere

Touch: `camera_array_collector.py`, `pipeline.py`. `asyncio.wait_for`
around `submit_one()` and solver iter with `3 × exp_time` deadline.
Prevents single stuck await from freezing the whole pipeline (we hit
this once today — `_wait_image_ready` Exposing→Idle path stalled
something downstream for 5 min until manual restart).

## Concrete next step

If resuming, do Phase 1 first. It's ~30 lines, no behavior change,
gives us the foundation. The operator approved the four-phase
direction; the data-model commit is uncontroversial.

```
1. Add PulseEvent dataclass to state.py (above PipelineState).
2. Add active_pulse: PulseEvent | None = None to PipelineState.
3. In enforcer._apply, after t_N_ms/t_E_ms are clipped:
   - take fresh snapshot
   - compute predicted as today
   - build PulseEvent(issued_utc=dt_utcnow_array(),
                      motion_end_utc = issued + active_total_ms,
                      settled_utc = motion_end + post_pulse_settle,
                      src_pos = snap.acquired_pos,
                      predicted_pos = predicted,
                      pulse_t_n_ms = t_N_ms, pulse_t_e_ms = t_E_ms)
   - state.update(active_pulse=event, predicted_pos=predicted)  # both for now
4. Commit, push, deploy, verify the field appears in NATS state.
```

## Production access

```
ssh poweruser@services01.oca.lan
cd ~/src/ocabox-tcs && git pull
~/.local/bin/poetry install --extras "cli guider oca" --sync
sudo systemctl restart oca_guider_jk15

# UI:
cd ~/src/ocabox-guider-ui && git pull
PATH=$HOME/local/node/bin:$PATH npx ng build --configuration production
rm -rf /storage/poweruser/node_modules/ocabox-guider-ui
mv node_modules /storage/poweruser/node_modules/ocabox-guider-ui
ln -sfn /storage/poweruser/node_modules/ocabox-guider-ui node_modules
```

Health check: `curl -sS http://services01.oca.lan:8090/healthz`.
Logs: `sudo journalctl -u oca_guider_jk15 -f --no-pager`.

Operator URL: http://services01.oca.lan:8090/ (port 8080 is held by
`model-stars-presence` uvicorn — do not touch).

Disk on `/` is 10 GB. Heavy state (venv, node_modules, thumbs, caches)
must live on `/storage` (98 GB) — see deploy README.

## Recent commits

```
f9e1220 guider: phase 1 of timing rework — PulseEvent dataclass + active_pulse field
616600d doc: session handoff with timing-architecture proposal and phased rollout
c60e0e8 guider: fix NameError in enforcer predicted_pos block
3cbed41 guider: bracket-box narrow search + miss budget grace + drop-oldest queue
c3c1419 guider: predicted_pos centers narrow search on post-pulse target
142f68c deploy: services01 production stack + FFS diagnostic logging
51edccf server conf and health  (operator's, before this session)
```

## Phase 1 deploy status

**Committed + pushed (`f9e1220`); deploy on services01 pending.**

The OCA LAN was unreachable from the dev machine when phase 1 landed
(operator on a plane, gateway not responding from outside). Phase 1 is
a no-behaviour-change commit — adds the `PulseEvent` dataclass and the
`active_pulse` field, populated by Enforcer alongside the existing
`predicted_pos`, cleared by Controller in lock-step. Existing
behaviour unchanged; the field is invisible to current consumers and
just appears in NATS state messages for inspection.

Whoever resumes runs:

```
ssh poweruser@services01.oca.lan
cd ~/src/ocabox-tcs && git pull
~/.local/bin/poetry install --extras "cli guider oca" --sync
sudo systemctl restart oca_guider_jk15
```

To verify, read a state message after a pulse fires (e.g. flip
mode→guiding briefly) and confirm `active_pulse` populates with
`{issued_utc, motion_end_utc, settled_utc, src_pos, predicted_pos,
pulse_t_n_ms, pulse_t_e_ms, correction_dx_px, correction_dy_px}`. If
yes — phase 1 done, phase 2 (the actual fix that consumes the field
in the Solver) is next.

## Open questions for operator

These came up but aren't blockers — flag for the resumer:

1. **`thumbnailHttpBase` / `thumbnailPathPrefix` UX**: the auto-discovery
   only overrides default values; once anything was set, it sticks
   forever. Operator agrees this is wrong — discovery should always
   win, with an explicit "📌 pin manual URL" toggle for proxy/VPN
   cases. ~10 min UI work, deferred.

2. **`variant: services01-jk15`** in prod thumbnail config — operator
   noted this should drop the `-jk15` suffix once a second telescope's
   guider colocates on services01 (thumbnail server is per-filesystem,
   not per-telescope). YAML comment flags the future rename.

3. **Alpaca camera contention**: operator said *"don't blame other
   programs"* — meaning the second-Alpaca-client warnings shouldn't
   kill our pipeline. Phase 4 (bounded waits) addresses the symptom;
   the root robustness needs deeper thought (forced abort + restart
   exposure on consecutive Exposing→Idle without imageready).
