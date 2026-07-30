# Night report — 2026-07-29/30, jk15 BESO guider, observer: Stefan Kimeswenger

Source material: Stefan's two screen-recordings + screenshots
(https://universe.uibk.ac.at/s/AfTjEkqaGpgw5Fw), his e-mail, and the full
`oca_guider_jk15.service` journal from services01 (20:00 UTC → 07:00 UTC,
35 144 lines; extended to 11:40 UTC for the morning tail). All times UTC.
Chile local = UTC−4.

Deployed version that night: `feat/guider_svc @ 2abb627` ("ver bump",
= working-tree v1.3.0-fiber lineage).

---

## 1. Session timeline (logs ⇄ videos)

| UTC | Source | What happened |
|---|---|---|
| 22:37:00 | log | method → `single_star` — session begins in `[single][monitoring]` |
| 22:41–22:52 | log | Stefan explores: several mode flips (monitoring↔guiding↔off), method flips single↔fiber. Matches video 1 preamble (help panel, left-click select, manual pulses) |
| 22:54:13–22:56:53 | **video 1** | `[single][monitoring]`, 3 bright stars; selects target; tries 1 px manual pulse; switches to `[fiber]` — FIBER CONFIG panel appears and **pushes MANUAL PULSE out of view** (his words: despair); sets min pulse with key `1`; arrow-up overshoots badly |
| 22:56:53 | log+video | mode → `guiding` while `[fiber]` — **star slowly driven AWAY from the fiber hole**: correction grows monotonically dy −0.25 → −2.1 → −5.1 → −8.3 → −12.2 px in ~11 s while E-pulses grow 27→156 ms (positive feedback, §2.1). Star exits the analysis window; corrections collapse to noise level |
| 22:58:29 | log+video 2 | back to `[single][monitoring]`; wants Beta Cen, which sits **outside the wide-search circle** (runtime r=125 px) |
| 22:58:33–23:00:11 | log | repeated `narrow miss budget exceeded … demoting to wide` — selection drops (`acquired lost` 23:00:11 in UI journal), Stefan waits, confused, nothing happens |
| ~22:59–23:00 | video 2 | manual 10 px pulses walk the star into the circle → auto re-acquire; zoom in; complains: **overlay lines scale with zoom, can't see anything**; yellow cross meaning unclear |
| 23:00:46 | log+video 2 | method → `fiber_photocentroid` (uses it mostly as a *viewfinder* for the hole) — green lock circle **wanders off the star, ADU 50k → 100 → 5** (noise-lock, §2.2) |
| 23:01:04 | UI journal (snap) | mode monitoring → guiding, still `[fiber]` — star near centre but lock is on noise; guiding pulls the *noise photocentroid* to the hole, star does not go in |
| 23:01:23–27 | log+video 2 | method → `single_star`; loop acquires the real star (~50 px off-fibre) and **holds it in place** — Stefan expected it to move the star *into* the fibre, gives up. End of video 2 |
| 23:02–23:25 | log | `[single][guiding]` left running; corrections noisy, growing (dx swings ±10–26 px around 23:12–23:15 — field disturbed, likely manual telescope interventions fighting the loop) |
| **23:25:19** | log | **camera frame freezes.** From here every cycle sees byte-identical data: correction locks at exactly `dx=+17.92 dy=+4.63` |
| 23:25 → 09:49 | log | **frozen correction re-issued as N=230 ms + E=54 ms pulses every ~5.4 s for 10.5 hours** (~110 pulses/10 min, ~7 000 total). This is the "service works in nowhere and miscontrols the telescope": runaway stars in TOI/ASA manual guiding, line-streaked stars on the Andor frames |
| 23:54, 00:07 | screenshots | Stefan's UI on jk15-tcu shows **disconnected**; NATS address reads `ws://192.168.38:9222` (malformed — missing `.7.`). His OFF clicks never reached the service; no mode command appears in the log between 23:01:04 and 09:49 |
| ~00:00–01:00 | e-mail | Stefan tries `sudo systemctl stop tcsd@services01-jk15` (unit **does not exist** — copied from our own quickref §7) and the `nats` CLI (**not installed** on services01 — also from quickref §7). Both dead ends. Gives up, no BESO data, aborts imaging too |
| 05:20–05:39 | log | 43 × `aput_pulseguide failed … (2002) Error creating value` — mount refusing pulses (likely parked / tertiary moved / ADR6 off). Guider keeps hammering regardless |
| 09:49:23 | log | `mode=off` received (morning intervention); `Pipeline mon subscription paused`. Pulsing ends |

Camera-contention background noise all night: **5 135** warnings
`Exposing→Idle without imageready=true … proceeding to fetch the buffer
anyway` — consistent with a second Alpaca client and/or firmware quirk,
and the direct enabler of the frozen-frame incident.

---

## 2. Root causes (code-level, confirmed)

### 2.1 Fiber mode drives the star AWAY from the hole — sign inversion
`fiber_photocentroid.py:248` emits `dx = −(photocentroid − reticle)`.
`single_star.py:453` emits `dx = pos − anchor`.
The pulse-guide model (`pulse_guide.py:75-80`) is explicit: `predict()`
expects the **raw error** `(star − anchor)` and internally computes the
cancelling pulse (`motion = −error`). Fiber's extra negation makes the
model *reinforce* the error: each pulse pushes the star further out.
Video 1 @ 22:56:53 is a textbook exponential divergence.
Root enabler: `Correction` docstring documents **no sign convention**
("Translation correction in pixel space"), so the two methods diverged.

**Fix:** drop the negation in fiber; document the convention in
`Correction`; add a regression test that both methods agree on a
synthetic off-centre star.

### 2.2 Fiber "acquires" pure noise (green circle at 5 ADU)
The detection gate compares `total_flux = Σ clip(window − bg, 0)` against
`kσ√n`. Clipping breaks the zero-mean assumption: pure noise sums to
`≈ 0.399·n·σ` (expected value of a half-normal), while the gate is only
`3σ√n`. For the 31×31 window: noise ≈ 384σ vs gate ≈ 93σ — **noise passes
4× over threshold**. Hence `acquired=yes` with mean ADU ≈ 5, the lock
circle wandering over empty sky, and (in guiding) pulses issued against a
noise photocentroid.

**Fix:** compute the gate on the *unclipped* sum `Σ(window − bg)` (zero-
mean under noise → √n statistics valid); keep the clipped weights for the
centroid only. Optionally raise `adu_sigma_threshold`.

### 2.3 Frozen frame → 10.5 h of blind pulsing (SAFETY)
`alpaca.py:242-278` (`_wait_image_ready`): on the `Exposing→Idle` fallback
it fetches the buffer anyway, assuming *"if bytes are stale, the solver
will simply produce no detection"*. False — a stale buffer contains a
perfectly detectable (old) star. From 23:25 the same buffer was re-served
every cycle with a fresh timestamp (`frame_age=0ms`), producing a
bit-identical correction that the Enforcer dutifully executed ~7 000×.

**Fixes (defence in depth):**
1. **Duplicate-frame detection** — hash a strided subsample of each
   array; identical hash ⇒ not a new frame ⇒ no Correction, count it,
   DEGRADED status after N repeats.
2. **Guiding watchdog** — in `guiding`, auto-demote to `monitoring` +
   ERROR event when: correction identical within ε for N cycles, or no
   fresh acquisition for T minutes, or cumulative issued pulse time per
   axis exceeds a sanity budget without error reduction.
3. **Pulse-failure latch** — K consecutive `aput_pulseguide` failures
   (e.g. the 2002 storm at 05:20) ⇒ demote to `monitoring`, ERROR.
4. **Mount/light-path gate (Stefan's suggestion — adopt)** — before each
   pulse (cached, e.g. 10 s): mount connected, tracking, not parked;
   optionally tertiary at ADR6 for BESO. Not satisfied ⇒ don't pulse,
   surface DEGRADED "light path not configured for guiding".

### 2.4 OFF that doesn't stop anything — dead UI, silent
The service never received an `off` (log gap 23:01→09:49). Stefan's UI
was disconnected (malformed NATS ws address `ws://192.168.38:9222`), and
the UI let him click OFF with no effect and no feedback.

**Fixes (UI):** hard "DISCONNECTED" overlay state; command buttons
disabled or visibly queued+failed when the socket is down; every command
must be confirmed by observed state change (optimistic UI forbidden for
mode changes); validate/normalise the NATS URL field.

### 2.5 Manual pulse ×10 too big
Keys `1/2/3/4` = 200/500/1000/2000 ms. Calibrated Jacobian: ~26 ms ≈ 1 px.
So the *smallest* keyboard pulse moves ~8 px — matches "even 1 overshoots
by a factor of 10". The UI px-selector minimum (10 px) is likewise far
too coarse for fibre work (fibre radius = 5 px).

**Fixes:** re-map durations to ~20/50/200/1000 ms; better: denominate in
px through the inverse Jacobian; px-selector minimum → 1 px. Split
keyboard semantics for px-step vs duration (shift-layer), document in help.

---

## 3. UX findings (video-sourced, for ocabox-guider-ui)

1. **Overlay stroke widths & labels scale with zoom** → unusable when
   zoomed (Stefan's 1st point). Draw in screen space (constant px).
2. **FIBER CONFIG panel displaces MANUAL PULSE** in `[fiber]` — exactly
   when manual nudging is most needed. Manual-pulse controls must stay
   visible in every method; fiber config collapsed/on-demand.
3. **Click outside wide-search circle** silently fails/loses selection —
   blink the circle red + toast explaining the constraint instead.
4. **Yellow cross** (predicted position?) confuses in non-guiding modes —
   show only in `[single][guiding]`, or label it.
5. **Green centre line** implies "guider is taking the star to centre"
   even in plain hold-position guiding — draw only when the actual target
   is the reticle (fiber mode / drop-to-reticle in flight).
6. **Fiber-entrance zoom inset** is useful as a viewfinder — make it a
   standalone toggle next to the zoom buttons, independent of method.
7. Wide-search circle radius: runtime slider showed 125 px while config
   default is 600 px — reconcile, and show the active value in the UI.
8. Help is read under pressure (Stefan consulted it repeatedly) — keep it
   short but add: mode×method semantics table, what `guiding` does NOT do
   (it holds; `drop → reticle` centres), pixel-vs-duration keys.

---

## 4. Documentation defects that burned the observer

- `Observer_Quickref_Guider.md` §7 named a **non-existent systemd unit**
  (`tcsd@services01-jk15`; real: `oca_guider_jk15.service`) and a
  **non-installed** `nats` CLI as the stop procedures. Stefan followed
  both to the letter at 1 a.m. Fixed 2026-07-30.
- No mention that ssh user on services01 is `poweruser` (he tried
  `observer@`).
- Keyboard table listed the 200–2000 ms mapping with no px equivalence.
- Fiber mode was described as "experimental, opt-in" but not
  *operationally dangerous*; until 2.1/2.2 ship it must be "do not use".

---

## 5. Action list

**P0 — safety (service, before next observing night)**
- [ ] Duplicate-frame detection (2.3.1)
- [ ] Guiding watchdog (2.3.2)
- [ ] Pulse-failure latch (2.3.3)
- [ ] Fiber sign inversion fix + Correction convention docs + tests (2.1)
- [ ] Fiber noise-gate fix (2.2)

**P1 — mount/light-path awareness (operator-approved 2026-07-30)**
- [ ] Tracking/parked/ADR6 pre-pulse check (2.3.4)
- [ ] **Auto-off on tertiary (M3) leaving the guider's light path** —
      subscribe to the relevant NATS telemetry/status subject and,
      when the tertiary position no longer matches the configured
      value (ADR6 for jk15 BESO), demote to `off` (camera dark, no
      pulses — the guider has physically lost its light feed, nothing
      to monitor). Config sketch per pipeline:
      ```yaml
      light_path_guard:
        enabled: true
        subject: <tertiary position subject for jk15>   # TBD — check tic telemetry
        expected: "ADR6"
        action: "off"          # off | monitoring
        grace_s: 10            # debounce transient reports
      ```
- [ ] **Auto-suspend on mount SLEW** — a commanded slew (from TOI /
      plan runner / hand paddle) invalidates the lock and any pulse
      in flight. Watch mount state (`slewing` flag via tic, or the
      relevant NATS subject) and demote `guiding → monitoring` when a
      slew starts; operator re-arms after the new field settles.
      Optionally auto-re-acquire when `tracking` returns and the
      wide-search finds a star (conservative default: stay in
      monitoring, journal entry explains why).

**P1 — UI (ocabox-guider-ui)**
- [ ] Disconnected-state hard overlay + command gating (2.4)
- [ ] Screen-space overlay strokes (3.1)
- [ ] Manual-pulse visible in fiber; fiber config collapsible (3.2)
- [ ] Manual pulse steps: px-denominated / 1 px minimum (2.5)
- [ ] Wide-search click feedback (3.3)

**P2 — UI polish**
- [ ] Yellow-cross visibility rules (3.4), centre-line semantics (3.5),
      fiber-inset toggle (3.6), wide-search value display (3.7),
      help rewrite (3.8), keyboard layers px/ms

**Day-after deployment & validation (2026-07-30 12:00–13:00 UTC)**

v1.3.3 deployed to services01 and tested on the closed telescope
(constraint honoured: guiding never engaged, zero mount commands;
camera use only).

- Service healthy: clean restart, RPC surface answers, mode
  off↔monitoring transitions + method swaps work, `guide_anchor`
  cleared on `off`. No pulses issued at any point (journal-verified).
- **FrameDeduplicator validated LIVE against the real failure**: the
  BESO camera driver is *currently wedged* — two independent 0.5 s
  exposures (own `startexposure` each, no errors reported) returned
  **byte-identical 2.35 Mpx arrays (0 differing pixels)**. The guider
  detected it (`Camera appears FROZEN: 10 consecutive identical
  frames`), dropped every stale frame and starved the solver — under
  v1.3.2 tonight's session would have re-acquired the frozen blob and
  pulsed blind again. This is almost certainly the same wedge mode
  that started at 23:25 UT during the incident.
- `imageready` **never** asserts on this driver (chronic; the
  Exposing→Idle fallback fires on literally every frame) — explains
  the night's 5 135 warnings. Harmless with dedup in place, but worth
  raising with the camera-server author.
- Fiber sign + gate live checks: **inconclusive** — the wedged camera
  delivered no fresh frames to the solver in fiber mode. Both are
  unit-regression-locked; re-run the live check after the camera
  server restart (scene even provides a convenient saturated blob
  ~31 px E of the reticle for a no-mount sign test).
- **Follow-up found (P0.5)**: while frames are frozen, the last
  `acquired` state stays in place — the UI shows a live-looking lock
  (ADU 65504 blob) minutes after the last real frame. After N
  consecutive duplicates the pipeline should invalidate `acquired`
  (or at least publish DEGRADED + a `stale_frames` flag) so the
  operator sees the freeze, not a healthy-looking lock.
- Camera recovery: the wedged component is the **guider camera's
  Alpaca server on `jk15-ccd`** (Windows host). ⚠ The
  `server_restart@jk15-beso` / `camera_restart@jk15-beso` ssh aliases
  (BESO Camera Interface §5) control the **spectrograph science
  camera** and are NOT applicable — an easy and dangerous confusion
  (the guider component is *named* `guider_beso`, the science-camera
  host is *named* `jk15-beso`). The same mix-up sat in the observer
  manual §6 since May (fixed 2026-07-30). Sanctioned guider-camera
  restart procedure: **TBD with operator** (RDP to jk15-ccd / Windows
  service restart / PDU?) — document it in maintainer docs once
  known, never in the observer manual.

**Camera recovery & final validation (2026-07-30 afternoon)**

- RS restart on jk15-ccd did NOT revive the ASI cameras (both ZWO
  profiles threw NRE on connect — wedge was at USB/driver level).
  **Host reboot of jk15-ccd fixed it**: guider camera live (content
  varies with exposure), and the chronically-broken `imageready` now
  asserts correctly (0.63 s / 1.88 s) — it was part of the same wedge.
- ⚠ RS restart came back with **Andor cooling OFF + setpoint 0** —
  operator re-applied. Add to any RS-restart procedure: verify
  `cooleron`/`setccdtemperature` immediately after.
- Soft per-device reconnect (`PUT connected=false→true`) tested on the
  wedged camera: reconnect blocked the RS ~40 s, first exposure after
  it **crashed the whole RS** (Andor included). Verdict: no gentle
  recovery exists; ladder documented in
  `MAINTAINER_guider_camera_recovery.md`.
- Phantom `ASI Camera (2)` profile (`ASCOM.ASICamera2_2`) confirmed as
  a second ZWO driver profile; still returns all-zero frames after
  reboot. Remove at a service window.
- **P0.6 found & fixed**: with live frames, the fiber gate passed on
  horizontal **banding** (~40 ADU "acquisition" on a dark region) —
  line-correlated readout noise violates the iid assumption behind
  √n statistics. Fix: per-row + per-column median **destriping** of
  the analysis window (kills bands and gradients, leaves a compact
  star; saturated pixels NaN-masked out of the medians), noise from
  the destriped MAD. Validated against a live dark banded frame
  (rejects at dark spot and at reticle-with-nothing-there); star
  cases regression-locked in unit tests (39 green).
- The "saturated blob ~31 px from reticle" seen earlier was the
  frozen buffer's content — the live dark frame has no such source;
  the on-sky positive validation of fiber (sign + acquisition)
  remains for the next clear evening.

**P0.7 + final deploy (2026-07-30 evening)**
- After the host reboot the healthy camera exposed a fencepost race:
  the driver does not clear ``imageready`` on ``startexposure``, so the
  protocol trusted the stale flag ~180 ms after each fresh frame and
  re-fetched the previous buffer — perfectly alternating
  fresh/duplicate frames, dedup absorbing 50% of exposures. Fix
  (v1.3.5): don't trust ``imageready`` before 0.75×exp_time.
  Live result: 60 s monitoring → **0 duplicates, 0 fallbacks,
  0 errors, full cadence** (50 cycles/60 s @ 0.5 s exp).
- Branch test debt cleared: all 5 stale tests updated to current
  contracts (int32 txn wrap, fallback log wording, narrow-miss budget
  ×2, manual-pulse anchor invalidation) — guider suite 129/129.
- Manual iteration 3 (operator's review notes): fiber allowed in
  monitoring (magnifier inset), single-vs-fiber hold-target semantics,
  wide-search adjustability at the click step, neighbour-star guiding
  recipe, fibre-entrance recalibration procedure (flat lamp →
  coordinates → maintainer), last-resort incident reporting, "leave it
  in off" note.

**Done 2026-07-30**
- [x] Log analysis, this report
- [x] P0 service fixes implemented + tested (fiber sign & gate,
      FrameDeduplicator, Enforcer repetition guard + failure latch,
      `Controller.safety_demote`); 37 tests green — NOT yet deployed
- [x] Manual **rewritten** (mkdocs admonitions, Stefan-informed):
      `oca-manual/docs/beso_guider.md` + identical canonical copy
      `Observer_Quickref_Guider.md`. Policy change: **UI-only stop
      procedure** — the SSH/nats "last resort" section is gone
      (observers escalate to maintainer instead; the service side is
      made safe by the watchdogs + P1 light-path/slew guards).
      Origin of the bad section traced: written 2026-05-20 (fcf3e03)
      with a confabulated unit name never validated on the host —
      lesson: emergency procedures must be executed on the target
      host before publication.
- [x] Reply to Stefan drafted (`email_draft_stefan_2026-07-30.md`)
- [x] UI/UX feasibility study (`UI_UX_FEASIBILITY_2026-07-30.md`)
