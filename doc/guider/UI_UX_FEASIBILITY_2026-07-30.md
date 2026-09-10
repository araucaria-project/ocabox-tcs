# Guider Web UI — UX Fixes Feasibility Study

**Date:** 2026-07-30 · **Driver:** observer session (Stefan, night 2026-07-29) ·
**Repo:** `~/projects/astro/ocabox-guider-ui` (Angular 21, standalone components, signals, Tailwind) ·
**Status:** read-only analysis, no code modified. Line numbers refer to working-tree state at commit `626b6a8`.

## Summary table

| # | Item | Primary files | Effort | Risk |
|---|------|---------------|--------|------|
| 1 | Overlay widths/text constant under zoom | `frame-view.component.ts` (overlay computeds :850-853 + template sweep) | **M** | Low — single choke point exists; hardcoded sizes need sweep; verify at zoom 16× |
| 2 | Manual pulse always visible (fiber) | `guider-dashboard.component.ts` :192-216 | **S** | Low — column already scrolls; fixes silent keyboard-feedback loss too |
| 3 | Pulse px steps ≥1 px; keyboard 1-4 re-denomination | `pulse-pad.component.ts` :157, `app.component.ts` :131, :212-215 | **S** | None — keys 1-4 currently write a **dead signal**; nothing to break |
| 4 | Reject click outside wide-search circle | `frame-view.component.ts` :890-894, :181-193; `guider-dashboard` :447-450 | **S/M** | Medium — a click outside the circle onto a real star is server-valid; strict reject removes capability (mitigations below) |
| 5 | Yellow cross (guide anchor) visibility | `frame-view.component.ts` :245-270 | **S** | Low |
| 6 | Green line semantics (only when target = reticle) | `frame-view.component.ts` :296-307 | **S** | Low — interacts with 5; drift chart unaffected |
| 7 | Fiber-entrance inset as standalone toggle | `frame-view.component.ts` :491-565, :420-474 | **S** | None |
| 8 | Disconnected-state handling | `nats.service.ts` :35-38, :57-126, :175-177; `app.component.ts` :29-47; `guider-dashboard` :506-513; `mode-toolbar` | **M** | Medium — reconnect/JetStream-consumer expiry edge case; confirm-echo needs care |
| 9 | Wide-search radius display coherence | `camera-panel.component.ts` :94-105, :174-178; `frame-view` :181-193 | **S** (M with dirty-preview circle / config-default from server) | Low — root cause of the 125-vs-600 was server state, not UI rendering |
| 10 | Help panel restructure | `app.component.ts` :72-108, :133-153 | **S** | None — must be updated anyway by items 3 & 6 |

Estimated total: roughly 2–3 focused days including visual verification against a live guider.

---

## Rendering architecture map (read this first — items 1, 4, 5, 6, 7 all touch it)

Everything lives in **one component**: `src/components/frame-view.component.ts`.

```
<div #host>                                (screen space, overflow-hidden)
  <div [style.transform]="translate(pan) scale(zoom)">    ← CSS pan/zoom wrapper (:60-63, :826-829)
     <img [src]=thumbnail  object-contain>                ← JPEG frame (:64-70)
     <div> frame-meta text overlay </div>                 ← HTML, :83-106  ⚠ inside transform
     <div> phase pill </div>                              ← HTML, :118-130 ⚠ inside transform
     <div> "fiber" badge </div>                           ← HTML, :137-143 ⚠ inside transform
     <svg viewBox="0 0 sensorW sensorH">                  ← ALL geometric overlays (:145-416)
        candidates circles / wide-search circle / fiber disc+ring /
        reticle (ReticleComponent) / guide-anchor X / acquired marker+ADU text /
        green error line / narrow-search box / trajectory arrow / scale bar / hover reticle
     </svg>
  </div>
  zoom buttons (:420-474), zoom label (:477-481)           ← screen space
  subraster inset — second, independent <svg> (:491-565)   ← screen space, own viewBox
  coord readouts (:567-580)                                ← screen space
</div>
```

Key mechanics:

- **Overlays are SVG 2D**, not canvas, not per-element CSS. The SVG `viewBox` equals sensor
  pixels, so every overlay coordinate is already sensor-space — no client/scale math.
- **Zoom/pan is a CSS transform** on the wrapper div (`view = {zoom, panX, panY}` signal, :635;
  `zoomBy()` :944-966; non-passive wheel listener :858-861). Zoom range 1–16 (:30-31).
- **Click→sensor mapping** goes through `svgPoint()` (:977-988) using inverse `getScreenCTM()`,
  which *does* absorb the ancestor CSS transform — clicks are correct at any zoom.
- **Overlay sizing helper — the load-bearing utility for item 1:** four computeds at
  :850-853 centralise sizes in sensor px:
  `overlayStrokePx` (=maxDim/500), `overlayCrossPx` (/22), `overlayMarkerPx` (/50),
  `overlayLabelPx` (/55). Most (not all) draw sites consume these.
- **`vector-effect="non-scaling-stroke"` is applied everywhere but does not do what the
  comment (:837-849) believes.** Per spec/implementation it compensates only the SVG's own
  user-space→viewport transform (the viewBox). The ancestor **CSS `scale(zoom)` is applied
  afterwards to the rendered result** and fattens strokes proportionally. This is precisely
  Stefan's "lines get fat and cover the star" symptom. Text has no non-scaling equivalent at
  all, so labels scale too.
- The reticle is a separate attribute component `g[appReticle]`
  (`src/components/reticle.component.ts`), fully parameterised by `len`/`stroke` inputs —
  it inherits any fix to the sizing computeds for free.
- Fiber inset is an independent second `<svg>` with its own tight viewBox — unaffected by
  main-view zoom.

---

## 1. Overlay line widths & text constant in screen pixels under zoom

**Current implementation.** As mapped above: sizes come from the four computeds (:850-853),
strokes carry `vector-effect="non-scaling-stroke"`, and the design comment (:837-849)
explicitly states overlays are *meant* to grow with zoom ("at high zoom the overlays remain
visible relative to the now-larger image features") while believing strokes stay constant.
Field experience contradicts both: strokes scale (CSS-transform loophole above) and grown
markers obscure the star.

Draw sites and their sizing source:

| Overlay | Lines | Sizing |
|---|---|---|
| Candidate circles | :162-175 | `overlayMarkerPx/StrokePx` ✅ computed |
| Wide-search circle | :181-193 | `overlayStrokePx` ✅ (radius is data, stays sensor-space) |
| Fiber disc + analysis ring | :203-225 | `overlayStrokePx` ✅ (radii are data) |
| Central reticle | :230-239 | `overlayCrossPx/StrokePx` ✅ via ReticleComponent |
| Guide-anchor broken X | :245-270 | ⚠ **hardcoded** ±12/±3 sensor px + computed stroke |
| Acquired marker + ADU `<text>` | :273-294 | `overlayMarkerPx/LabelPx` ✅ — text scales with zoom |
| Green error line | :296-307 | `overlayStrokePx` ✅ |
| Narrow-search box | :318-333 | stroke computed; box size is data ✅ |
| Trajectory arrow + endpoints | :340-371 | computeds ✅ |
| Scale bar + `<text>` | :373-394 | 100 px line is *deliberately* sensor-space; label via computed |
| Hover reticle | :397-415 | ⚠ **hardcoded** r=14, ±22/±6 |
| Dash patterns | :190, :221, :330, :526 | ⚠ hardcoded sensor-space `stroke-dasharray` |

Also: the HTML frame-meta overlay, phase pill and fiber badge sit **inside the transformed
wrapper** (:83-143) — they scale with zoom and drift off-screen with pan (pan is clamped to
negative offsets, so the image's top-left corner leaves the viewport). Almost certainly
unintended; the coord readouts at :567 already document the correct pattern ("in screen
space, not transformed").

**Minimal clean fix** (stay in sensor-space SVG, divide by zoom — no re-architecture):

1. Make the four computeds zoom-aware: `overlayStrokePx = base / this.view().zoom` (etc. ×4).
   Since `view` is a signal, all overlays re-render on zoom automatically. One-line each.
2. Sweep the two hardcoded geometry sites (guide-anchor X, hover reticle) onto the computeds
   (e.g. anchor arms = `overlayMarkerPx()*0.9` … gap `*0.25`).
3. Bind the `stroke-dasharray` values to a computed (`overlayDashPx()`), or accept dash
   scaling as cosmetic.
4. Keep the scale bar's 100 px line length in sensor units (it is the scale reference); only
   its stroke/label divide.
5. Move the frame-meta/phase-pill/fiber-badge HTML blocks out of the transformed wrapper to
   sibling screen-space position (same classes, different parent).
6. Optionally drop the now-redundant `vector-effect` attributes (harmless either way), and
   correct the misleading comment block :837-849.

**Effort: M** (the mechanism is S; the sweep + visual verification at 1×/4×/16× on both
methods is the bulk). **Risks:** at 16× the reticle becomes 16× smaller in sensor units —
sub-pixel floats are fine in SVG; verify the fiber disc outline (data-sized) vs its stroke
still reads well. Screenshot-compare before/after; no state or protocol interaction.

**Rejected alternative:** drawing overlays in a screen-space sibling SVG (outside the
transform) with manual sensor→screen mapping. Cleanly solves text too, but duplicates the
transform math the CSS currently does for free and touches every draw site — not minimal.

## 2. Manual pulse panel vs fiber config

**Current implementation.** It is not "pushed out of view" — it is **replaced**.
`guider-dashboard.component.ts` :192-216 is an either/or branch:
`@if (isFiberMode()) { fiber config panel } @else { manual pulse pad }`, with a comment
(:192-197) documenting the deliberate (now field-falsified) decision "in fiber mode there are
no individual manual pulses". Consequences beyond the visible one: the `pulsePad` viewChild
(:239) is `null` in fiber mode, so keyboard-arrow pulses (which still work — `app.component`
:200-211 calls `dashboard.manualPulsePx()` directly, bypassing the pad) get their RPC
success/failure feedback dropped silently in `manualPulsePx()` (:494-504,
`this.pulsePad()?.reportRpc(...)` no-ops).

**Minimal clean fix.** Render the pulse pad unconditionally; wrap the fiber panel in a
collapsed-by-default disclosure:

- Replace the `@if/@else` with two sibling sections: manual pulse always, then
  `@if (isFiberMode())` a `<details>` (or a heading-button + signal, matching house style —
  no `<details>` used elsewhere; a `fiberOpen = signal(false)` + chevron header is more
  consistent) containing `<app-fiber-config-panel>`. Persist open state in localStorage
  (pattern exists: `RETICLE_KEY` :16, pulse-pad mode :160-167).
- Fiber panel component itself needs no change (`fiber-config-panel.component.ts` already
  guards on `method === 'fiber_photocentroid'`, :134-149).

**Effort: S.** **Risks:** right-column height — already handled, the column is
`xl:max-h-full xl:overflow-y-auto` (:157). Side benefit: restores keyboard-pulse feedback in
fiber mode. Interaction with item 3 (same panel) — do together.

## 3. Pulse step sizes and keyboard denomination

**Current implementation.**

- Pad presets: `pxPresets = [10, 30, 100, 200]` (`pulse-pad.component.ts` :157); the free
  number input **already allows 1 px** (`min="1" step="1"`, :99). ms presets
  `[200, 500, 1000, 2000]` (:156). Pad default mode is **px** (:178, "image-axis is the more
  intuitive choice"), emitting `pulsePx` → `store.pulsePixels()` → `pulse_pixels` RPC
  (`guider.store.ts` :386-388). ms mode emits `pulse` → `manual_pulse` RPC.
- Keyboard arrows: `app.component.ts` :200-211 — always **px-mode** via
  `dashboard.manualPulsePx()` with step from `arrowStepPx = signal(30)` (:131). The comment
  at :197-199 claiming "step size from the px-pad signal so changing it in the UI also
  changes the keyboard step" is **wrong** — the two are independent.
- Keys 1/2/3/4 (:212-215) set `pulseDurationMs` (:125) — **a dead signal: nothing reads
  it**. Arrows use px; the pad's ms buttons use the pad's own `duration` signal. The
  duration presets survive only inside the pad's ms mode (:51-57). The help table
  (:136) still advertises "1/2/3/4 → pulse duration 200/500/1000/2000 ms" — misleading.

**Minimal clean fix.**

1. `pxPresets` → `[1, 5, 10, 30]` (or `[1, 2, 5, 10, 30]` with a 5-col grid); fiber hole
   r = 5 px makes 1–5 px the working range. No server change: `pulse_pixels` takes floats;
   sub-threshold moves are handled by the existing min-pulse policy (skip/snap_half/stochastic,
   commit `9e661a9`) — worth a one-line note in the pad hint text (:136) that a 1 px request
   may be skipped/snapped by the mount-side minimum.
2. Re-denominate keys 1/2/3/4 → `arrowStepPx.set(1|5|10|30)` — replaces the dead writes,
   loses nothing. Update help rows accordingly ("1/2/3/4 → arrow step 1/5/10/30 px").
3. Optional coherence (recommended, still S): make the keyboard read the pad's step —
   replace `arrowStepPx()` with `this.firstDashboard()?.pulsePad()?.pixels() ?? 30` via a
   small accessor on the dashboard, and have keys 1-4 set the pad signal. One source of
   truth; the pad highlights the active preset as keyboard feedback. (Requires item 2 so the
   pad exists in fiber mode.)
4. Shift-layer for ms (`⇧1..4` → pad `duration` 200/500/1000/2000) is trivial to add in the
   same `switch` (check `ev.shiftKey` on the digit cases) — only worth it if operators
   actually use ms mode; defer otherwise.

**Effort: S.** **Risks:** none technical. UX risk of 1 px default step being mistaken for
"broken" (nothing visibly moves) — mitigated by keeping default 30 and the min-pulse note.

## 4. Click outside wide-search circle → reject + red blink + message

**Current implementation.** Click path: `frame-view.onSvgClick` (:890-894) → `lockAt` output
→ `dashboard.lockAt()` (:447-450) → `store.lockAt()` (:368-370) → `lock_at` RPC.
Server side (`ocabox-tcs/src/ocabox_tcs/services/guiding_svc/controller.py:228-296`):
`lock_at` accepts **any** coordinates and returns ok — it just seeds
`acquired_pos=(x,y), acquired=True`. The failure is downstream and silent: next frame the
solver runs *narrow* search in a `search_reg_px` box around the click
(`single_star.py:285-287`); if no star is there the lock is lost, and re-acquisition wide
search is **hard-filtered to the wide circle around `central_point`**
(`single_star.py:315-324`) — so the selection snaps back inside the circle. The RPC status
line even shows "lock_at ok". The wide circle itself is drawn at frame-view :181-193
(suppressed in fiber mode).

**Important nuance:** a click outside the circle **onto a real star is valid** — narrow
search around the click will find it and the lock holds. The circle constrains only
*re-acquisition*. A strict client-side reject would remove that capability.

**Minimal clean fix** (in `FrameViewComponent`, no server change):

1. In `onSvgClick`, when `!isFiberMode()`, compute
   `d = hypot(pt - s.central_point)`; if `d > s.wide_search_radius_px`:
   - set `searchCircleFlash = signal<number>` (timestamp); bind the circle's stroke to red
     (`[class]`/`[attr.stroke]` + a ~1 s CSS keyframe blink — add the keyframe to
     `styles.css`, which currently has none; the only existing animation is `anim-spin`
     referenced by the sci-fi reticle);
   - surface a transient message. **There is no toast infrastructure** — the closest thing
     is the camera-panel RPC status line (proven too subtle, see item 8). Recommend a small
     `notice = signal<{text; kind} | null>` rendered screen-space bottom-centre inside
     frame-view with a 4 s auto-clear — deliberately built so item 8 can reuse it.
   - **decide reject vs warn**: (a) strict reject per the stated goal (don't emit `lockAt`)
     — simplest, loses the outside-star capability; (b) candidate-aware: reject only if no
     entry of `s.candidates` lies within `search_reg_px/2` of the click, otherwise send —
     preserves capability, still catches the "clicked on nothing" case. (b) is ~15 extra
     lines and is the better fix; candidates are refreshed every frame (full-frame
     detection, `single_star.py:264-283`).
2. Message text should explain the *consequence*: "outside wide-search circle — if lock is
   lost here it will re-acquire inside the circle" (warn variant) or "click inside the
   dashed circle, or enlarge wide search r in CAMERA" (reject variant).

**Effort: S** (strict) / **M** (candidate-aware). **Risks:** fiber mode has no circle and no
candidates — skip the guard (arguably `lock_at` should be suppressed in fiber mode entirely;
flag for the server team). Keep the guard tolerance-free of the dirty slider value (use
active state, see item 9).

## 5. Yellow cross visibility rules

**Current implementation.** Two amber overlays can read as "yellow cross":

- **Guide-anchor broken X** — frame-view :245-270, rendered whenever `s.guide_anchor` is
  non-null. `guide_anchor` is server-owned: snapshot of `acquired_pos` on mode→guiding, null
  otherwise (`guider.store.ts` :73-78); `lock_at` clears it (`controller.py:287`);
  `drop_to_reticle` sets it to `central_point`. Despite the template comment "Only shown in
  guiding mode", the render condition is only field-truthiness — any stale/edge publication
  draws it, and after `drop_to_reticle` (and in fiber+guiding) it sits *exactly on the
  reticle*, duplicating it confusingly. It is unlabeled.
- **Predicted-position amber ring + trajectory line** — :340-371, gated on
  `active_pulse && (in_flight | settling)`. Correctly scoped and transient; probably not the
  complaint, but also unlabeled.

**Minimal clean fix.** Tighten the anchor gate and label it:

```
@if (s.guide_anchor && s.mode === 'guiding' && anchorOffReticle()) { … + <text> "anchor" }
```

where `anchorOffReticle()` = distance(guide_anchor, central_point) > ~1 px — suppresses the
redundant X after drop-to-reticle and in fiber mode (where the anchor is definitionally the
fibre). Add a small amber `<text>` label using `overlayLabelPx()` (constant-size after
item 1). Optionally label the predicted ring "predicted" the same way.

**Effort: S.** **Risks:** hiding the anchor when it coincides with the reticle removes the
only visual confirmation that a drop landed — item 6's fixed green line takes over that job
(the error line pointing at the reticle communicates "guiding to reticle"). The status bar
already prints anchor coordinates with a tooltip (`status-bar.component.ts` :31-35) as a
fallback. Do 5 and 6 together.

## 6. Green line / dot semantics

**Current implementation.** frame-view :273-307: whenever `s.acquired && s.acquired_pos`,
the green acquired marker (+ ADU label) is drawn **and** a green line from
`central_point → acquired_pos` (:296-307, comment "visible drift error"). The line is drawn
in **all modes and methods** — in monitoring, and in guiding-while-holding-position (anchor
= locked star, not the reticle), it falsely implies "the guider is moving the star to
centre".

**Minimal clean fix.** Make the line truthful — draw it only when there is an active
correction target, and point it at the *actual* target:

- Hide in `monitoring`/`off` (no correction happens; the drift chart is the drift display).
- In `guiding`: endpoint = `s.guide_anchor ?? s.central_point`; render only when the
  target is meaningfully separated from `acquired_pos` (> ~1 px). With the item-5 rule this
  yields exactly the requested behaviour: line visible when
  `guide_anchor == central_point` (fiber mode; post drop-to-reticle) — i.e. "pulling star to
  reticle"; when holding position (anchor == star) the line collapses to nothing, which is
  the honest picture.

Concretely: replace the `x1/y1` bindings at :298-299 with a
`guideTarget = computed(...)` and wrap the `<line>` in
`@if (s.mode === 'guiding' && guideTarget(); as tgt)`. The green acquired dot/circle itself
stays in all modes (it means "lock held", which is correct).

**Effort: S.** **Risks/interactions:** the drift chart's baseline (`driftAnchor`,
`guider.store.ts` :217-247) is an independent client-side reference — untouched. Item 5 must
land together so operators don't lose both anchor cues at once. Update help (item 10) with
the new semantics.

## 7. Fiber-entrance zoom inset as standalone toggle

**Current implementation.** The subraster magnifier (:491-565) is a second SVG whose viewBox
is a `subrasterHalfPx()`-sized window around `central_point` (:766-781), reusing the same
image URL (browser cache). Render gate: `@if (isFiberMode() && displayedUrl())` (:491) —
hard-coupled to method. It internally repeats fiber-specific overlays (disc, analysis ring,
photocentroid dot) plus a tiny reticle cross.

**Minimal clean fix.**

1. `showInset = signal<boolean>(loadFromLocalStorage)` + toggle button appended to the
   existing zoom-control stack (:420-474) — same 7×7 button style; icon: magnifier-square.
2. Gate becomes `@if (showInset() && displayedUrl())`.
3. Inside the inset, keep the fiber disc/ring gated on `isFiberMode()`; the reticle cross
   (:530-545) and acquired marker (:547-556) already make sense for every method. Fallback
   half-size when not fiber: `analysisRadiusPx()` defaults to 15 → ±22 px window; acceptable,
   or clamp to a fixed ±20 px when `!isFiberMode()`.
4. Migration nicety: initialize the signal to `true` the first time fiber mode activates if
   the operator has never touched the toggle (one localStorage sentinel), so current fiber
   users see no regression.

**Effort: S.** **Risks:** none; inset is screen-space and independent of main-view zoom.
Minor overlap risk with the scale bar at bottom-left — inset already sits at `bottom-12`
above it.

## 8. DISCONNECTED state handling

**Current implementation.**

- Connection state: `nats.service.ts` signals `isConnected / isConnecting / connectionError`
  (:35-38). `connect()` (:57-126) fails fast on first attempt (`waitOnFirstConnect:false`,
  5 s timeout) but *after* a successful connect reconnects forever silently
  (`maxReconnectAttempts:-1`); a status watcher (:100-112) sets `connectionError` on
  disconnect and clears it on reconnect — **`isConnected` is never flipped back to false on
  a mid-session disconnect**, only `connectionError` changes.
- Visibility: header shows only a 2×2 px dot (grey when offline) + msgs counter
  (`app.component.ts` :31-37). `connectionError` renders **only inside the connect dialog**
  (`connect-dialog.component.ts` :74-76), which auto-collapses once a guider appears
  (app.component :160-169). So Stefan's session: bad URL (`ws://192.168.38:9222` — 3-octet
  host; the browser treats it as a hostname, DNS fails) → initial connect throws after 5 s →
  error only in the (possibly collapsed) dialog → dashboards keep rendering the **last
  JetStream-replayed state** looking fully alive.
- Command dispatch: mode buttons (`mode-toolbar.component.ts` :45-67) are never disabled and
  their active highlight comes from server state (:148-160) — so there is **no optimistic
  reflection** (good), but stale green "guiding" + a swallowed click is indistinguishable
  from success. The dispatch (`guider-dashboard.runRpc` :506-513 →
  `nats.rpcRequest` :175-177 throws `'not connected'`) reports failure **only to the
  camera-panel status line** (:511) — an 11 px grey-panel message nowhere near the button
  clicked.

**Minimal clean fix (four independent pieces):**

1. **Unmissable banner** (S): in `app.component`, a fixed top full-width red banner when
   `!nats.isConnected() || nats.connectionError()`: "NATS DISCONNECTED — commands
   disabled (<url>)" with a Reconnect button opening the dialog. Requires also fixing
   `isConnected` on mid-session drop: in the status watcher set `isConnected.set(false)` on
   `disconnect` and `true` on `reconnect` (2 lines, `nats.service.ts` :104-110).
2. **Disable/guard commands** (S): pass `connected = nats.isConnected()` into
   `app-mode-toolbar` (add `[disabled]` on mode/method/acquire/drop buttons) and short-circuit
   `runRpc` / `manualPulse*` in the dashboard with the item-4 transient notice ("not
   connected — command not sent") instead of the camera-panel line. Keyboard shortcuts route
   through the same dashboard methods, so they inherit the guard.
3. **URL validation** (S): in `connect-dialog.apply()` (or `nats.connect()` normalization,
   :58-62): parse with `new URL(target)`; require ws/wss scheme; if the hostname is
   digits-and-dots, require exactly 4 octets 0-255 — flag `192.168.38` with "looks like an
   incomplete IPv4 address" before attempting. Keep it a warning-with-confirm, not a hard
   block (hostnames are legal).
4. **Confirm-echo watchdog** (S/M): the RPC response already confirms server receipt (`runRpc`
   checks `status === 'ok'`), so the pure echo case is mostly covered; the residual gap is
   "RPC ok but state stream dead" (e.g. expired JetStream consumer). In
   `dashboard.setMode()`: after ok, `setTimeout(5s)` checking `state()?.mode === target`,
   else notice "mode change confirmed by RPC but no state update received — state stream may
   be stale (reconnect?)". Clear the timer on state arrival.

**Effort: M** total. **Risks/interactions:** the JetStream push consumers use
`inactiveEphemeralThreshold` = 60 s (:215) — after an outage > 60 s the server deletes them
and `nats.ws`'s socket reconnect does **not** recreate them (the `subscribed` set is only
cleared in `connect()`, not on reconnect events): the UI would show "connected" with a
frozen state stream. The watchdog (4) detects it; the proper fix (re-attach subscriptions on
the `reconnect` status event, or force a full `connect()` cycle) should ride along — flag as
a required sub-task of this item. Don't disable the *connection* dialog controls themselves.

## 9. Wide-search radius display

**Current implementation.** One state field, two consumers:

- Drawn circle: frame-view :181-193, radius bound to live `s.wide_search_radius_px` — always
  the **active** value.
- Slider: `camera-panel.component.ts` :94-105 (`min=50 max=900 step=10`), value =
  sparse-override-else-upstream (`valueSearchR`, :174-178); dirty state highlighted amber
  (:97-99). Label reads "wide search r (px)"; the value itself is a bare number.

So the UI was internally consistent on Stefan's night: slider 125 = active state 125; the
600 lives only in YAML config, and the server does not publish the config default (only
`central_point_default` gets that treatment, `guider.store.ts` :64-67). Root cause is a
server-state-vs-config question (why did the pipeline boot/persist 125?) — worth a separate
server-side check. Residual UI gap: while the slider is dirty (unapplied), slider and circle
legitimately diverge with only the amber tint as a clue.

**Minimal clean fix.**

1. Append units to the value readout (`{{ valueSearchR() }} px`) and add a
   `title` explaining "radius of the dashed circle around the reticle — wide-search /
   re-acquisition region". (Trivial.)
2. **Dirty-preview circle**: pass an optional `previewSearchRadius` input into
   `app-frame-view` from the dashboard (plumbed from the camera panel's dirty override —
   needs a small output or public computed on `CameraPanelComponent`); render a second amber
   dashed circle while dirty. Operator sees exactly what Apply will do. (~30 lines across
   3 files.)
3. **Config default surfacing** (needs server): publish `wide_search_radius_default` in
   `PipelineState` alongside the existing `central_point_default` pattern
   (`guiding_svc/state.py`), show "default 600" as a small reset chip next to the slider.
   S on both sides but crosses repos — schedule with the next tcs release.

**Effort: S** for 1–2; **M** overall if 3 is included. **Risks:** none; item 4's guard must
use the *active* value (it does — reads `state`), noted there.

## 10. Help panel restructure

**Current implementation.** Inline in `app.component.ts`: modal template :72-108 (backdrop +
two `<table>`s), data as two flat arrays `shortcuts` (:133-147) and `mouseShortcuts`
(:149-153). Toggled by `?` key (:188-191) and header button (:43-46); Escape closes (:192-193).

**Proposed restructure** (content-driven, structure-light):

1. Extract a `HelpPanelComponent` (`src/components/help-panel.component.ts`) — app.component
   is already the largest non-dashboard file and items 3/5/6 all change help content; a
   dedicated component keeps churn localized. Mechanically trivial (move template + arrays,
   one `closed` output).
2. Three sections, in this order:
   - **Mode × method semantics table** — static 3×3-ish grid: rows off/monitoring/guiding ×
     columns single/fiber (multi = "soon"), cells one short phrase ("watches drift, no
     correction", "pulls star to anchor", "pulls star into fibre"…). Include the one-liner
     "what guiding does NOT do": *does not recenter the star unless you drop → reticle; it
     holds the star where it was locked*. This directly encodes the item-5/6 confusion.
   - **Keys — px layer**: arrows + 1/2/3/4 step presets (post-item-3 denomination),
     r/g/m/o/h, zoom, d, tab.
   - **Keys — duration layer** (only if the ⇧-layer from item 3 is adopted) + mouse table.
3. Keep it terse: the current two-column key/desc table style is right; the only structural
   addition is the semantics grid (a third static array of row objects).

**Effort: S** (content + extraction). **Risks:** none; must be sequenced **after** items 3,
5, 6 so it documents the new behaviour, not the old.

---

## Cross-cutting notes

- **No toast/notification infrastructure exists.** Items 4 and 8 both need one; build a
  single minimal transient-notice mechanism (signal + timed clear + screen-space chip,
  either per-frame-view or app-shell level) and reuse it. Avoid pulling in a library.
- **The overlay-sizing computeds (frame-view :850-853) are the screen-space helper** the
  task asked about — they exist and are the single choke point that makes item 1 mostly a
  4-line change plus sweep. The `svgPoint()` inverse-CTM helper (:977-988) is the
  corresponding input-side utility and needs no change.
- **Sparse-override panel pattern** (camera-panel/fiber-config, documented at
  `camera-panel.component.ts` :14-28) is the house style for any new settings UI (item 9
  preview, item 7 toggle persistence via the existing localStorage pattern).
- **Suggested implementation order:** 8 (safety) → 2+3 (panel) → 1 (rendering) → 4 → 5+6 →
  7 → 9 → 10 (docs last).
- Repo working tree is clean at `626b6a8` on the UI side; memory notes indicate
  `v0.4.0-fiber` era work — confirm branch/tag state before starting edits.
