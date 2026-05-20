# Guider session handoff — 2026-05-15

Resume reading order:
1. **`doc/guider/STATE_MACHINE.md`** — canonical logic, anchor/pulse
   lifecycles. Source of truth. Read first.
2. **`doc/guider/ARCHITECTURE_REVIEW_2026-05-14.md`** — roadmap, full
   feature list (B1-B10, U1-U13).
3. This file — what's done, what's next, where we left off.

## Production state

- **Host**: `services01.oca.lan` = `192.168.7.45` (operator off-site
  on VPN; resolves by IP only). Next on-site: 2026-10.
- **Branch in production**: `feat/guider_svc` for ocabox-tcs,
  `master` for ocabox-guider-ui.
- **Stable tags** (just cut): `v1.2.0-fl2-pre-fiber` (tcs) and
  `v0.3.0-fl2-pre-fiber` (ui). Annotated tags carry full release
  notes (`git show <tag>`). Operator can checkout these for clean
  observations if trunk gets messy.
- **Deploy URL**: http://192.168.7.45:8090/ (single port; UI served
  from `/`, thumbnails from `/thumbs/`, healthz at `/healthz`).
- **Thumbnails**: 968×608 q80 ≈ 250 KB per frame (VPN-friendly).

## What works (validated this session)

- Anchor lifecycle (per STATE_MACHINE.md):
  - lock_at clears anchor; bootstrap fills it with refined centroid
    (lock_at = picks STAR, not where to drag it).
  - manual_pulse clears anchor + active_pulse + predicted_pos.
  - drop_to_reticle clears stale pulse plan, sets anchor=central_point.
  - Wide-recovery distance gate: ≤ 2×search_reg_px → same star, anchor
    unchanged; further → reset to recovered position.
- Phase 2 timing model (PulseEvent, frame_phase classification,
  bracket-box in ACQUIRING).
- Phase 4 robustness (bounded waits, drop-oldest queues).
- UI: discovery wins over localStorage; load-serialised image swap;
  phase pill (PULSING/SETTLING/ACQUIRING); trajectory arrow during
  pulse motion; tabular-nums overlay with fixed columns.
- Half-res thumbnails for VPN.

## Post-handoff session (2026-05-15 afternoon) — fiber-mode implementation

Status: code complete, awaiting first-sky tuning. Not deployed yet —
operator opens VPN to push the build.

**Backend (`ocabox-tcs` working tree on `feat/guider_svc`, v1.3.0-fiber)**:
- `FiberPhotocentroidMethod.solve()` fully implemented per agreed math
  (see "Heuristic" block below). Smoke-tested against synthetic
  Gaussian PSFs at 9 offsets — dead zone, ramp, full regime all
  correct; sign convention (correction pulls star toward reticle)
  verified in both axes and on diagonals.
- Runtime method-swap via `set_state({"method": "...", "method_params":
  {...}})`. Controller detects the method change against the previous
  state, calls `Pipeline.swap_method()`, which instantiates the new
  method from the registry and hot-swaps it on the live Solver.
  Method-internal state is discarded on swap (acceptable — fiber has
  no internal state; single_star has only a narrow-miss counter).
- `fiber_method_params:` block added to both
  `config/guider.jk15.guider_beso.yaml` and
  `config/services01.guider_jk15.yaml` as informational defaults the
  UI can copy when switching.

**UI (`ocabox-guider-ui` working tree on `master`, v0.4.0-fiber)**:
- Method radio in mode-toolbar: `[single][multi—soon][fiber]`. multi
  is disabled with tooltip. Active selection highlighted in sky blue
  (distinct from mode's emerald/amber).
- Layout refactor — three-row toolbar (method | mode + re-acquire +
  calibrate | drop+home+reticle); right column reorganised as camera
  → diagnostics → manual-pulse-or-fiber-config → calibration → journal.
  Diagnostics and journal moved out of the middle column (which keeps
  drift chart + RMS only).
- Fiber-mode visual on frame-view: filled semi-transparent black disc
  at `central_point` of radius `fiber_radius_px`, dashed sky-blue
  analysis-window ring, FIBER badge bottom-right, candidate circles
  hidden, search-radius circle hidden, narrow-search box hidden,
  view-stars toggle disabled with tooltip.
- Subraster magnifier inset (bottom-left of frame-view, 160×160 px),
  HARPS-N-inspired. Visible only in fiber mode. SVG-based — reuses
  the same image URL (browser cache), centred on `central_point`,
  scaled to show ±1.5×analysis_radius_px. Repeats key overlays
  (fibre disc + analysis ring + tiny reticle + photocentroid marker)
  at the magnified scale. Scale label below ("±N px").
- Fiber config panel (replaces manual-pulse pad when fiber is active):
  sliders for `fiber_radius_px`, `analysis_radius_px`,
  `hole_zone_factor`, `adu_sigma_threshold`. Sparse-overrides pattern
  (Apply / Reset). Tooltips repeat the operational summary of each
  knob so the operator can tune without leaving the UI.

**Pending (deferred until first-sky):**
- Tuning knob values (the operator wants to find good
  `hole_zone_factor` on real BESO data).
- Parking-lot items (drift markers in monitoring, overlay clutter at
  centre, Apply UX diff, exclusion zones, etc.) — see "Parked open
  items" below.

### Simulation regime — `config/sim.guider+ui.yaml`

End-to-end test loop without TIC, mount or real camera. Brings up the
guider against a synthetic 512×512 PSF (sub-pixel jitter built into
`FileSimProtocol`) plus a colocated `thumbnail_svc.server` that serves
both the Angular UI and the JPEG stream from the same port.

Verified during the 2026-05-15 sim run on localhost NATS:
- `enable_observatory: false` + `service_mode: false` start the
  service cleanly even with no telescope/mount handles wired (TIC
  warnings then continue).
- `manual_pulse` returns `{status: no_mount}` cleanly.
- `set_state({method, method_params})` from the UI radio swaps the
  live solver method at runtime — confirmed both single→fiber and
  fiber→single. Right-panel auto-swaps between "FIBER CONFIG"
  sliders and "MANUAL PULSE" pad. Journal logs the swap event.
- Subraster magnifier renders fibre disc + analysis ring + tiny
  reticle + photocentroid marker at the magnified scale.
- Fiber dead-zone gating works: with a star jittering ~1 px inside a
  3 px hole, `last_correction_dx_px/dy_px` stay None (no correction
  emitted), exactly the agreed behaviour.

**Stream provisioning is now enforced at startup** (fix landed
2026-05-15 alongside this work): `NatsConn._verify_streams()` probes
every publish subject via `$JS.API.STREAM.NAMES` during service open.
Uncovered subjects → service refuses to start with a clear error
naming the subjects and pointing at the oca_nats_config remedy.
Rationale: silent-drop is *worse* than an outage — the operator
otherwise sees a happy-looking service while no data reaches the UI.

To provision streams on a local dev NATS, run the oca_nats_config
updater:
```bash
cd ~/projects/astro/oca_nats_config && poetry run build
# or
~/projects/astro/oca_nats_config/.venv/bin/python -m oca_nats_config.main
```
That ships `svc_publish`, `svc_status`, `svc_heartbeat`, `svc_registry`
(plus all `tic_*` streams).

**`svc.telemetry.*` cleanup 2026-05-15**: the startup probe initially
also covered `svc.telemetry.{guider}.{instance}.pipeline.{pipe}.correction`
and `svc.telemetry.{guider}.{instance}.active.correction`, and flagged
their absence (no `svc_telemetry` stream in `oca_nats_config`).
Investigation revealed the whole namespace was **dead surface**:
`correction_publisher` / `camera_active_correction_publisher` factory
methods existed in `nats_conn.py` but were *never called*; the subjects
were *advertised* in the discovery metric, but nothing ever wrote a
message and the UI never subscribed (per-pulse markers come from
`enforcer_pulse` *events* on the `svc.publish.…events` subject; the
state message carries `last_correction_dx/dy_px` which is what the UI
actually reads). Decision: drop the dead surface entirely rather than
implement publish-side or leave the silent-drop liability lurking.

Cleanup (2026-05-15):
- Removed `correction_publisher`, `camera_active_correction_publisher`,
  `telemetry_subject` from `nats_conn.py`.
- Removed `correction` / `active_correction` entries from the
  discovery metric in `manager.py`.
- Removed matching fields from `GuiderInfo.subjects` /
  `PipelineInfo.subjects` in the UI's typed interface.
- Stripped the two telemetry probes from `_verify_streams` — now
  covers exactly the 4 subjects in active use (state, events,
  journal, thumbnail_ready).
- Updated the file header subject scheme to drop the
  `<prefix>.telemetry.*` line.
- Deleted `svc_telemetry` from local NATS (not needed).

`svc_telemetry` is no longer expected anywhere — neither in
`oca_nats_config` nor in any running service. If we later want a
high-resolution correction telemetry channel, we'll wire it up
properly at that point (publisher + UI subscription + stream
provisioning all in one PR).

**One bug found+fixed during sim**: frame-view hard-coded sensor
dimensions to 1936×1216 (BESO). The sim 512×512 sensor displayed
the reticle at upper-left and made the fibre disc too small to see.
Fixed by deriving `sensorWidth/Height` from the thumbnail
notification's `dimensions` field (auth source — reflects actual
post-binning frame shape). Inputs preserved for parents that know
the geometry ahead of the first thumbnail.

## What's next (operator-approved priority)

### Method registry + Fiber mode (B2 in roadmap) — IMPLEMENTED 2026-05-15

Empty stub exists at
`src/ocabox_tcs/services/guiding_svc/stages/solver/methods/fiber_photocentroid.py`.
Registry already maps `"fiber_photocentroid"` → class. Need to
implement `.solve()` per operator-agreed math:

**Heuristic (operator-confirmed 2026-05-15)**:

```python
# Window around reticle
window = extract_window(frame.array, reticle, analysis_radius_px)
bg, noise = robust_bg_stats(window_edges)
net = (window - bg).clip(min=0)
# Saturation mask — drop pixels ≥ saturation_adu (PSF wings dominate centroid).
net[window >= saturation_adu] = 0
total_flux = net.sum()

# ADU gate (geometric, NOT flux-reference-calibrated):
if total_flux < adu_sigma_threshold * noise * sqrt(window_n_pix):
    return None  # star in hole or empty — no correction

# Photocentroid relative to reticle
pc_x = (net * x_offsets).sum() / total_flux
pc_y = (net * y_offsets).sum() / total_flux
d_apparent = sqrt(pc_x**2 + pc_y**2)

# Hole-bias compensation (piecewise linear)
fib_r = fiber_radius_px
full_at = fib_r * hole_zone_factor  # default 2.0
if d_apparent <= fib_r:
    d_corrected = 0.0                                 # dead zone (max damping)
elif d_apparent >= full_at:
    d_corrected = d_apparent                          # full regime (1:1)
else:
    t = (d_apparent - fib_r) / (full_at - fib_r)
    d_corrected = t * full_at                         # linear ramp

# Scale photocentroid vector to corrected magnitude
scale = d_corrected / d_apparent if d_apparent > 1e-3 else 0
correction.dx = -pc_x * scale  # pull toward reticle
correction.dy = -pc_y * scale
```

**Config (all to YAML under method_params)**:
- `fiber_radius_px: 5.0`
- `analysis_radius_px: 15.0` (= 3·fib_r typical)
- `adu_sigma_threshold: 3.0`
- `hole_zone_factor: 2.0` (tuning knob — lower = more aggressive, higher = more undershoot)
- `saturation_adu: 62000.0` (mirror single_star)

**Behavior at corners (verified by operator's geometric reasoning)**:
- Star deep in hole: total_flux < threshold → no correction. ✓
- Star peeking (apparent d ≈ fib_r): d_corrected = 0. ✓ (max damping anti-overshoot)
- Star at hole edge (d ≈ 1.5·fib_r): d_corrected ≈ fib_r. ✓
- Star fully out (d ≥ 2·fib_r): 1:1 correction. ✓
- Bright star with wide PSF (d underestimates real D): undershoot,
  converges over multiple cycles. Acceptable per operator: "undershoot
  nie jest tak szkodliwy, overshoot grozi brakiem zbieznosci".

### UI work (parallel)

- **Method radio** in mode-toolbar: `[single][multi—disabled][fiber]`.
  Multi shows tooltip "not implemented". Sends `set_method` RPC.
- **Layout refactor** (operator: "lepiej niz upychac"):
  ```
  ┌──────── Camera & Mode ────────┐
  │ Method  [single][multi—][fiber]│
  │ Mode    [off] [mon] [guide]    │
  │ [drop_to_reticle] [reticle home]│
  │ ─────────────────────────────  │
  │ exp · gain · binning           │
  │ diagnostics: acquired/fwhm/Δ/ADU│
  │ ─────────────────────────────  │
  │ Manual pulse (or fiber config) │
  │ ─────────────────────────────  │
  │ Journal                        │
  └────────────────────────────────┘
  ```
- **Fiber-mode visual**: in `frame-view.component.ts`,
  - Filled semi-transparent black disc of radius `fiber_radius_px` at
    `central_point` (represents the fiber hole).
  - Hide candidate circles (showCandidates toggle auto-disabled).
  - Hide narrow search-region box (no narrow search in fiber method).
  - Badge "FIBER" near phase pill.

### Subraster (future, not yet)

Operator's vision: when star drops into fiber, a small zoomed-in view
of the central region shows fine-detail wiggle. Inspiration: HARPS-N.
Layout TBD; might be a "magnifier" inset overlay on frame-view or a
separate panel. Defer to after fiber-mode lands.

## Parked open items (PS-list — minor but remember)

- **Drift chart markers in monitoring** don't move (PS2): mode/pulse
  markers only update after switching to guiding. Investigate event
  subscription path.
- **Overlay clutter at center** (center.png screenshot from operator):
  too many strokes overlap on the fiber hole. Options:
  - non-scaling-stroke instead of zoom-scaling.
  - "hide overlays" hold-button.
  - subtler reticle near center.
- **Subraster** as detailed above — design later.
- **Apply UX**: current vs pending value diff (Architecture Review U1).
- **Calibration as modal** (not collapsible panel) — Architecture
  Review U9.
- **State split (config / runtime)** — Architecture Review X2.
- **Auto-shutoff at UT** — B5.
- **Exclusion zones** — B3+U4.
- **Camera temperature + tracking-from-NATS indicator** — B7+B8.
- **FITS snapshot** — B6.

## Production access notes

SSH: `ssh poweruser@192.168.7.45` works. Host key for hostname
`services01.oca.lan` differs from IP — added IP to `~/.ssh/known_hosts`
with `accept-new` earlier. If session moves machines, may need to
re-accept.

GitHub private deps (ocabox, ocabox-common, pyaraucaria, ctc): handled
by `~/.git-credentials` stored PAT on services01. Poetry resolves them
transparently.

UI rebuild flow (critical: ng build BEFORE mv symlink — Node resolves
modules via real path, not symlinked path):
```
cd ~/src/ocabox-guider-ui && git pull
rm -f node_modules && rm -rf /storage/poweruser/node_modules/ocabox-guider-ui
PATH=$HOME/local/node/bin:$PATH npm ci && npx ng build --configuration production
mv node_modules /storage/poweruser/node_modules/ocabox-guider-ui
ln -sfn /storage/poweruser/node_modules/ocabox-guider-ui node_modules
```

Guider restart: `sudo systemctl restart oca_guider_jk15` — no pulses
issued; safe even mid-observation as long as operator isn't actively
guiding.

## Recent commits (last session)

```
fcb94da (ui)  bump 0.2.0 → 0.3.0 — tag v0.3.0-fl2-pre-fiber
1f95cb6 (tcs) bump 1.1.0 → 1.2.0 — tag v1.2.0-fl2-pre-fiber
543df18 (tcs) anchor lifecycle per STATE_MACHINE
6a174d1 (tcs) doc: canonical state machine
2258851 (tcs) lock_at + drop_to_reticle clear stale pulse plan
63fbe85 (tcs) wide-recovery stop resetting anchor (was aborting slews)
e56eb3f (tcs) drift anchor tracks monitoring too
65b72fa (tcs) phase 2 timing — TRACKING/IN_FLIGHT/SETTLING/ACQUIRING
de44651 (ui)  phase pill → bottom-centre
3850cc6 (ui)  phase 3 overlay (trajectory arrow + phase pill)
0246a72 (ui)  drive image advance from signal effect (not load callback)
8947bd0 (ui)  flex+gap overlay (columns don't pack)
fb4ca35 (ui)  split overlay: latest meta vs image lag
383716b (ui)  load-serialised image swap
6539118 (ui)  discovery wins + frame age overlay
bee4829 (tcs) half-res thumbnails 968×608 q80
0b9fbdc (tcs) public_host → 192.168.7.45 (VPN-resolvable)
59657aa (tcs) phase 4 robustness — bounded waits everywhere
f9e1220 (tcs) phase 1 — PulseEvent dataclass
```
