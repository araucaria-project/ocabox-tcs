<!-- Canonical source: ocabox-tcs/doc/guider/Observer_Quickref_Guider.md
     Keep the two files identical; this copy is what observers see on
     home.oca.lan. Rewritten 2026-07-30 after the first observer test
     (see ocabox-tcs/doc/guider/NIGHT_REPORT_2026-07-29_stefan.md). -->

# BESO Guider — Observer Quick Reference

*Appendix to the BESO Camera Interface document (Kimeswenger, v2.30).
This sheet covers operating the autonomous guider that locks the target
on the BESO fibre — a service running on `services01.oca.lan`, fed by
the dedicated guider camera on `jk15-ccd`. It does **not** cover the
spectrograph itself.*

---

## 1. What it is — the mental model

A server-side service that reads frames from the guider CCD, finds the
guide signal, and issues small `PulseGuide` corrections to the ASA
mount. You interact only through the web UI — nothing runs in your
browser; the browser just displays state and sends commands.

!!! info "Three things the guider does — and the one it doesn't"
    - It **holds** a star at a position *you* chose (the **anchor**).
    - On your explicit command **`drop → reticle`**, it slides that
      star onto the fibre.
    - It keeps holding it there for the rest of the exposure.

    It does **not** decide anything by itself. In particular,
    switching to `guiding` does **not** move your star to the fibre —
    it freezes the star **where it currently is**. If you skip
    `drop → reticle`, the guider will faithfully hold your star 50 px
    away from the fibre all night, working exactly as designed and
    delivering no spectrograph light.

The guider knows nothing about your science target — only pixels on
the guider camera.

---

## 2. URLs

| Resource      | URL                               |
|---------------|-----------------------------------|
| Guider web UI | `http://services01.oca.lan:8090/` |
| (IP fallback) | `http://192.168.7.45:8090/`       |

The link is also available from the OCM landing page:
`http://home.oca.lan/` → "BESO Guider".

!!! warning "Trust the connection dot, not the buttons"
    Every click is only as good as the NATS connection. **Before
    trusting any command, check the dot in the top bar is green.**
    A disconnected UI silently swallows clicks — the page still looks
    alive (last-known state), but nothing reaches the telescope.
    If the dot is not green: reload the page; verify the NATS address
    reads exactly `ws://192.168.7.38:9222` (every octet matters).

---

## 3. Start-of-night checklist

1. Tertiary at **position ADR06** (BESO path), cover open, dome open,
   target slewed and **tracking**.
2. Open the guider UI and confirm in the top bar:
    - green NATS dot;
    - the `jk15 / guider_beso` row exists, status `OK`;
    - live thumbnails arriving (≈ 2 Hz).
3. The pipeline starts in mode **`monitoring`** — frames flow, the
   solver runs, drift is plotted, **no pulses are sent**. Safe default.

---

## 4. The workflow — this is all you need

Method **`single`** (default), starting in `monitoring`:

1. **Left-click your target star** in the frame view. The solver
   refines your click to the star's centroid — this becomes the
   **anchor**, the position the loop will hold.
   The mount is *not* touched by selecting.
2. Switch mode to **`guiding`** (++g++). The loop now holds the star
   at the anchor — where it is *right now*, not at the fibre.
3. Press **`drop → reticle`**. The loop slides the held star onto the
   reticle (= fibre entrance) and keeps holding it there.
   The button needs `single` + `guiding` + `acquired = yes`.

That's the whole story for an ordinary observing night.

!!! tip "Sanity check"
    Steady-state drift RMS should settle to **≤ 1–2 px** per axis
    within ~30 s. If it *grows*, switch back to `monitoring` and
    contact the maintainer — do not fight it with manual pulses.

??? question "I clicked a star and nothing got selected — why?"
    Your star is outside the **wide-search circle** (drawn around the
    reticle; radius set by the *wide search r* slider). Selection only
    works inside it. Nudge the star into the circle with manual
    pulses, or move the telescope (in `monitoring`!), and the
    selection will engage. This limit exists so a re-acquisition after
    a cloud can never silently jump to a different star far away.

??? question "What are all the markers on the frame?"
    | Marker | Meaning |
    | --- | --- |
    | Cross-hair reticle | The fibre entrance (`central_point`). Fixed; your *destination*. |
    | Green circle + ADU label | The current lock — what the solver is holding this frame. |
    | Amber/yellow broken cross | The **anchor** — where the loop wants the locked star to sit. |
    | Big circle around reticle | Wide-search limit — clicks and re-acquisition work only inside. |
    | Small squares (key ++d++) | Every candidate the detector found this frame. |

    If the green circle is not on your star, the loop is not guiding
    on your star — re-select before trusting it.

### Method `fiber`

!!! danger "Do not use `fiber` — defective as of 2026-07-30"
    Live testing on 2026-07-29 confirmed two bugs: in `guiding` it
    drives the star *away* from the fibre, and its lock indicator can
    latch onto empty-sky noise (green circle wandering at a few ADU).
    Fixes are implemented and awaiting on-sky validation; this notice
    will be lifted afterwards. Until then `single` + `drop → reticle`
    covers every observing scenario.

### Method `multi`

Not implemented yet.

---

## 5. Keyboard & mouse

Keys work when the UI window has focus and no text input is active.

| Key | Action |
| --- | --- |
| ++g++ / ++m++ / ++o++ | mode → guiding / monitoring / off |
| ++r++ | re-acquire — force wide-search around target |
| ++tab++ / ++shift+tab++ | (single) cycle lock through detected candidates |
| ++arrow-up++ ++arrow-down++ ++arrow-left++ ++arrow-right++ | manual pulse, image-axis pixels (current step) |
| ++1++ / ++2++ / ++3++ / ++4++ | pulse duration preset → 200 / 500 / 1000 / 2000 ms |
| ++plus++ / ++minus++ / ++0++ | frame zoom |
| ++d++ | toggle detection-candidates overlay |
| ++h++ | reticle home — restore calibrated default (admin; rare) |
| ++question++ | full shortcut panel |

!!! warning "Pulse scale on jk15: the smallest preset moves ~8 px"
    The mount responds ≈ **26 ms per pixel**, so preset ++1++
    (200 ms) moves the star ≈ 8 px — bigger than the whole fibre hole
    (radius 5 px). For fine centring use the smallest **pixel** step
    in the MANUAL PULSE panel (down to 1 px), not the duration keys.
    Finer keyboard steps are on the roadmap.

Mouse:

- **Left-click** *(single)* — lock onto a star near the click.
  Mount untouched until `guiding`.
- **Right-click** — move the target reticle (**admin operation**;
  don't, unless you know exactly why).
- **Wheel** — zoom around cursor.

---

## 6. Do **not** share the guider camera

!!! danger "One camera, one client"
    The guider holds an exclusive ASCOM/Alpaca session on
    `jk15.guider_beso`. Pointing **any** other Alpaca/ASCOM client at
    it while the guider runs will collide: frames stutter, freeze or
    corrupt; the driver can wedge (`Object reference not set…`) until
    a camera restart (`camera_restart@jk15-beso`, §5.6 of the camera
    document); the guider log fills with `OCABOX error 2002`.

    A frozen camera is worse than a dead one: the guider may keep
    seeing the *last* frame. Rule: during science, the **only**
    consumer of the guider camera is the guider service. For a
    hand-driven preview, set the guider to `off` first (§7), do your
    work, then bring it back.

---

## 7. Stopping the guider at end of night

The guider runs on the server — **closing the browser does not stop
it.**

1. Set mode to **`monitoring`** (pulses stop immediately).
2. Then **`off`** — camera capture stops, the service idles.
3. **Verify**: the mode pill reads `off` and no new thumbnails arrive.

!!! warning "The verify step is not optional"
    A command from a disconnected UI goes nowhere (§2). If the mode
    pill does not change to `off` within a few seconds: check the
    connection dot, reload the page, and repeat. If the UI still won't
    respond, **contact the maintainer** (bottom of this page) — do not
    improvise on the servers.

---

## 8. Mount control during guiding

While in `guiding`, the service talks to the mount about once per
second. **Do not issue manual slews, hand-paddle jogs, or
SyncToCoordinates from another tool** — they compete with the guider's
pulses and the result is a runaway. To move the telescope, switch to
`monitoring` first.

The guider only ever issues `PulseGuide` of ≤ 1500 ms. It never slews,
parks, or syncs.

---

## 9. Quick troubleshooting

| Symptom | First thing to check |
| --- | --- |
| Buttons do nothing / state frozen | Connection dot + NATS address (§2). Reload the page. |
| Clicked a star, nothing selected | Star outside the wide-search circle (§4). |
| No thumbnails in UI | Thumbnail HTTP base in the Connection panel. |
| `drop → reticle` greyed out | Needs `single` + `guiding` + `acquired = yes`. Lock a star first, then switch to guiding. |
| No detection candidates | exp_time too low / sparse field — or method is `fiber` (no candidates by design); switch to `single`. |
| Green circle wanders off the star, ADU reads a few counts | You are in `fiber` (§4 — do not use). Switch to `single`, re-select. |
| Star locked but drift grows | Switch to `monitoring`, contact the maintainer (Jacobian calibration). |
| Camera `unavailable` / NRE | `camera_restart@jk15-beso`, then guider `off` → `monitoring`. |

---

## 10. Glossary

- **Method** — the solver strategy: `single` (default; locks one star
  you pick) or `fiber` (photocentroid around the reticle — currently
  disabled, §4). Changing method changes the meaning of every other
  control, so it sits above mode.
- **Mode** — the pipeline's intent: `off` (camera idle), `monitoring`
  (frames + solver, **no pulses**), `guiding` (active corrections).
- **Reticle** — fixed marker at the fibre entrance (`central_point`).
  Your destination; never moves by itself.
- **Anchor** — the pixel position the loop holds the star at. Created
  when you select a star; moved onto the reticle by `drop → reticle`.
- **Acquired** — solver confirms a guide signal at the expected spot.
  Required for `drop → reticle`; without it `guiding` has nothing to
  hold.
- **Pulse-guide** — short mount nudge (`N`/`S`/`E`/`W`, ≤ 1500 ms)
  issued to cancel measured drift.
- **Re-acquire** (++r++) — force a wide-search around the target after
  a bump, cloud, or lost lock.
- **Wide search** — the circle around the reticle within which stars
  can be selected and re-acquired. Radius: *wide search r* slider
  (config default 600 px).

---

*Maintainer:* `mkaluszynski@akond.com`. Service code:
`ocabox-tcs/src/ocabox_tcs/services/guiding_svc/`. Config:
`config/guider.jk15.guider_beso.yaml`.
