<!-- Canonical source: ocabox-tcs/doc/guider/Observer_Quickref_Guider.md
     Keep the two files identical; this copy is what observers see on
     the OCM manual site. Rewritten 2026-07-30 after the first observer
     test (ocabox-tcs/doc/guider/NIGHT_REPORT_2026-07-29_stefan.md);
     service/client architecture section added after the 2026-07-30
     mail exchange. -->

# BESO Guider — Observer Reference

*Appendix to the BESO Camera Interface document (Kimeswenger, v2.30).
This page covers operating the autonomous guider that holds the target
on the BESO fibre — a service running on `services01.oca.lan`, fed by
the dedicated guider camera on `jk15-ccd`. It does **not** cover the
spectrograph itself.*

---

## 1. Overview

The guider is a server-side service. It reads frames from the guider
CCD, locates the guide signal in each frame, computes the offset from
the target position, and issues short `PulseGuide` corrections to the
ASA mount.

Its scope is deliberately narrow:

- it **holds** a star at a position selected by the operator (the
  **anchor**);
- on the explicit command **`drop → reticle`** it moves that star onto
  the fibre;
- it continues holding it there.

It takes no decisions on its own. In particular, in the `single`
method switching to `guiding` does **not** move the star to the
fibre — it holds the star **where it currently is**. Placing it on
the fibre is the separate `drop → reticle` step (§5). A guider left
in `guiding` without `drop → reticle` will correctly hold the star at
its original position, delivering no light to the spectrograph.
(The `fiber` method is the one exception: there the hold target *is*
the fibre entrance itself — see §5.)

The guider has no knowledge of the science target — it operates purely
on guider-camera pixels.

---

## 2. Architecture: the service and its clients

Understanding this section prevents the most serious operating errors.

**The service** runs permanently on `services01.oca.lan`. It is the
only component that talks to the camera and the mount. Its operating
mode — `off` / `monitoring` / `guiding` — is a property of the
service, held on the server.

**The web UI is a thin client.** It displays the service state and
sends commands; nothing runs in the browser. Consequently:

- Closing the browser page has **no effect** on the service. A guider
  left in `guiding` keeps guiding after every browser in the
  observatory is closed.
- Whether a client is connected or disconnected **does not affect the
  service** in any way.
- Any number of clients may be open; a new one can be connected at any
  time from any browser at OCM (and remotely, with tunnelling).

**The connection indicator** (dot in the top bar: green = connected,
grey = disconnected) is therefore the first thing to check before
reading or clicking anything:

!!! warning "A disconnected client shows a stale snapshot"
    When the connection is lost, the UI keeps displaying the **last
    state it received** — including the mode indicator. A client that
    shows `off` while disconnected proves nothing about the service:
    the service may well be guiding. Commands clicked in a
    disconnected client are **not delivered** and are currently
    discarded silently (an unmissable disconnected indication is on
    the roadmap). Always confirm the dot is green before trusting the
    display or issuing commands; after a mode command, confirm the
    mode indicator actually changes.

If a client will not load or will not connect while the service is
known to be running, suspect the local computer first (browser cache,
the machine itself) and try a different browser or computer. The NATS
address in the Connection panel must read exactly
`ws://192.168.7.38:9222`.

---

## 3. URLs

| Resource      | URL                               |
|---------------|-----------------------------------|
| Guider web UI | `http://services01.oca.lan:8090/` |
| (IP fallback) | `http://192.168.7.45:8090/`       |

The link is also available from the OCM landing page:
`http://home.oca.lan/` → "BESO Guider".

---

## 4. Start-of-night checklist

1. Tertiary at **position ADR06** (BESO path), cover open, dome open,
   target slewed and **tracking**.
2. Open the guider UI and confirm in the top bar:
    - green connection dot;
    - the `jk15 / guider_beso` row exists, status `OK`.
3. The mode is whatever it was left at (it is service state, §2) —
   typically `off`. Set it to **`monitoring`** and confirm live
   thumbnails start arriving (≈ 2 Hz). In `monitoring` frames flow,
   the solver runs, drift is plotted, **no pulses are sent**.

!!! info "When you are not using the guider, leave it in `off`"
    In `off` the service does not touch the camera at all — no
    exposures, no load on the shared Alpaca stack. `monitoring` and
    `guiding` both keep the camera busy. End of session or a longer
    pause: switch to `off`.

---

## 5. Standard workflow

Method **`single`** (default), starting in `monitoring`:

1. **Left-click the target star** in the frame view — the click must
   land **inside the wide-search circle** (the large circle around
   the reticle); clicks outside it do not select. The circle's radius
   is adjustable (*wide search r* slider) — enlarge it to reach a
   star further out, shrink it to exclude neighbouring stars from
   automatic re-acquisition. The solver refines the click to the
   star's centroid — this becomes the **anchor**, the position the
   loop will hold. Selecting a star does not move the mount.
2. Switch mode to **`guiding`** (++g++). The loop now holds the star
   at the anchor — at its current position, not at the fibre.
3. Press **`drop → reticle`**. The loop moves the held star onto the
   reticle (the fibre entrance) and keeps holding it there. The
   button requires `single` + `guiding` + `acquired = yes`.

This sequence covers a standard observing night.

!!! tip "Convergence check"
    Steady-state drift RMS should settle to **≤ 1–2 px** per axis
    within ~30 s. If it grows instead, switch back to `monitoring`
    and contact the maintainer — do not compensate with manual pulses.

??? question "A click on a star does not select it — why?"
    The star is outside the **wide-search circle** (drawn around the
    reticle; radius set by the *wide search r* slider). Selection
    works only inside it. Either enlarge the circle with the slider,
    or bring the star into it with manual pulses / a telescope move
    (in `monitoring`) — selection then engages. The limit exists so
    that re-acquisition after a cloud passage can never silently jump
    to a distant star; conversely, in a crowded field you can shrink
    the circle so a neighbouring star can never be picked up by
    mistake.

??? tip "Program star in the fibre = weak guide signal? Guide on a neighbour"
    Once the program star is injected into the fibre, most of its
    light disappears into the hole and the residual halo can be a
    poor guide signal. If a suitable field star is visible in the
    frame, guide on that one instead:

    1. Put the program star on the fibre first (steps 1–3 above).
    2. **Left-click the neighbouring star** (enlarge the wide-search
       circle first if it sits outside). Re-selection re-anchors the
       loop to the new star **at its current position** — nothing
       moves, the program star stays in the fibre.
    3. The loop now guides on the bright neighbour; the program star
       is held on the fibre indirectly (rigid field geometry).

    Do **not** press `drop → reticle` after re-selecting — that would
    drag the neighbour into the fibre.

??? question "Frame overlay legend"
    | Marker | Meaning |
    | --- | --- |
    | Cross-hair reticle | The fibre entrance (`central_point`). Fixed; the destination. |
    | Green circle + ADU label | The current lock — what the solver is holding this frame. |
    | Amber broken cross | The **anchor** — where the loop keeps the locked star. |
    | Large circle around reticle | Wide-search limit — clicks and re-acquisition work only inside. |
    | Small squares (key ++d++) | All candidates the detector found this frame. |

    If the green circle is not on the intended star, the loop is not
    guiding on that star — re-select before proceeding.

### Method `fiber`

In the `fiber` method the solver tracks the photocentroid of all
light in a window around the reticle, and the hold target is the
fibre entrance itself (no star selection, no `drop → reticle`).
Switching to `fiber` also enables the **fibre-entrance magnifier
inset** — useful as a close-up view of the injection region.

!!! danger "Do not use `fiber` + `guiding` — awaiting on-sky validation"
    Testing on 2026-07-29 found defects in fiber-mode guiding (the
    loop drove the star *away* from the fibre; the lock indicator
    could latch onto noise). Fixes are deployed but not yet validated
    on sky — until this notice is lifted, **do not engage `guiding`
    while in `fiber`**.

    `fiber` + **`monitoring`** is safe and allowed: no pulses are
    sent, and you keep the magnifier inset for visual checks during
    manual work. `single` + `drop → reticle` covers the actual
    guiding for all observing scenarios in the meantime.

### Method `multi`

Not implemented yet.

---

## 6. Keyboard & mouse

Keys act when the UI window has focus and no text input is active.

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

!!! warning "Pulse scale on jk15: the smallest duration preset moves ~8 px"
    The mount responds ≈ 26 ms per pixel, so preset ++1++ (200 ms)
    moves the star ≈ 8 px — more than the fibre hole radius (5 px).
    For fine centring use the smallest **pixel** step in the MANUAL
    PULSE panel (down to 1 px), not the duration presets. Finer
    keyboard steps are planned.

Mouse:

- **Left-click** *(single)* — lock onto a star near the click. The
  mount is not moved until `guiding` is engaged.
- **Right-click** — move the target reticle (**administrative
  operation**; do not use during normal observing).
- **Wheel** — zoom around cursor.

---

## 7. Guider camera exclusivity

!!! danger "One camera, one client"
    The guider holds an exclusive ASCOM/Alpaca session on
    `jk15.guider_beso` (a small CMOS served from the `jk15-ccd`
    Windows host — **not** the BESO science camera). Any other
    Alpaca/ASCOM client pointed at it while the guider runs will
    collide: frames stutter, freeze or corrupt; the driver can wedge
    (`Object reference not set…`, or silently serving one frozen
    frame) until its camera server is restarted — a maintainer
    action; the guider log fills with `OCABOX error 2002`.

    During science observations the **only** consumer of the guider
    camera is the guider service. For a hand-driven preview, set the
    guider to `off` first (§8), do the work, then bring it back.

!!! danger "Do not confuse the two BESO cameras"
    The ssh commands `server_restart@jk15-beso`,
    `camera_restart@jk15-beso` etc. (BESO Camera Interface document,
    §5) control the **spectrograph science camera**. They are not a
    remedy for guider-camera problems and must not be run for guiding
    issues. The guider camera is served from `jk15-ccd`; its recovery
    is a maintainer action.

---

## 8. Stopping the guider at end of night

The service runs on the server — **closing the browser does not stop
it** (§2).

1. Set mode to **`monitoring`** (pulses stop immediately).
2. Then **`off`** — camera capture stops, the service idles.
3. **Verify**: the mode indicator reads `off` and no new thumbnails
   arrive.

!!! warning "The verification step is mandatory"
    A command sent from a disconnected client is not delivered (§2).
    If the mode indicator does not change to `off` within a few
    seconds: check the connection dot, reload the page or use another
    computer's browser, and repeat. If no client can connect while
    pulsing must be stopped, use the last-resort procedure below —
    and inform the maintainer either way.

??? warning "Last resort — stopping the whole guider service (avoid if possible)"
    This stops the entire guider **service** on `services01`: camera
    acquisition, mount corrections, thumbnail generation **and the
    web UI itself** (the UI page is served by the same service, so
    after stopping it the page will no longer load — this is
    expected).

    ```
    ssh poweruser@services01.oca.lan      # user: poweruser
    sudo systemctl stop oca_guider_jk15.service
    ```

    To bring the guider back:

    ```
    sudo systemctl start oca_guider_jk15.service
    ```

    The service restarts into a safe idle state (no pulses); the UI
    page becomes available again ~15 s later.

    Why this is a last resort and not a routine procedure:

    - the normal UI path achieves the same result (`off`) without
      shell access, and works from any computer at OCM;
    - a service stopped this way stays down until somebody remembers
      to start it — the next observer finds a dead UI page;
    - it requires shell access to a shared production machine, where
      a mistyped command affects unrelated systems.

    **Never reboot `services01` itself.** The machine hosts several
    other observatory services beyond the guider; rebooting it to fix
    a guider problem takes them all down. If stopping the unit above
    does not resolve the situation, contact the maintainer.

    **Report every use of this procedure**: note it in the night
    report / control-room log and mail the maintainer (address at the
    bottom of this page) with the time and what led to it. Needing
    the last resort means something upstream failed — it can only be
    fixed if it is known about.

---

## 9. Mount control during guiding

While the service is in `guiding`, it commands the mount about once
per second. **Do not issue manual slews, hand-paddle jogs, or
SyncToCoordinates from other tools** — they compete with the guider's
corrections and produce a runaway. To move the telescope, switch the
guider to `monitoring` first.

The guider only ever issues `PulseGuide` of ≤ 1500 ms. It never slews,
parks, or syncs.

---

## 10. Troubleshooting

| Symptom | First thing to check |
| --- | --- |
| Buttons have no effect / display frozen | Connection dot and NATS address (§2). Reload the page; try another browser or computer. |
| Mode shows `off` but the telescope reacts to the guider | The client is disconnected and shows a stale snapshot (§2). Reconnect (green dot), read the actual mode, set `off`, verify. |
| UI page does not load at all | Try another computer first. If it fails everywhere, the service (which also serves the page) may be down — contact the maintainer. |
| A click on a star selects nothing | Star outside the wide-search circle (§5). |
| No thumbnails in UI | Thumbnail HTTP base in the Connection panel. |
| `drop → reticle` greyed out | Requires `single` + `guiding` + `acquired = yes`. |
| No detection candidates | exp_time too low / sparse field — or method is `fiber` (no candidates by design); switch to `single`. |
| Green circle wanders off the star, ADU reads a few counts | Method is `fiber` (§5 — no `guiding` there until validated). Switch to `single`, re-select. |
| Star locked but drift grows | Switch to `monitoring`, contact the maintainer (pulse-model calibration). |
| Camera `unavailable` / NRE / `Camera appears FROZEN` in status | Guider-camera server on `jk15-ccd` needs maintainer attention. Do **not** run any `*@jk15-beso` command (§7 — that is the spectrograph science camera). |

??? note "Fibre-entrance recalibration after the guider camera was moved (maintainer-coordinated)"
    The reticle position (`central_point`) is a calibrated constant:
    the pixel where the fibre entrance sits on the guider sensor. Any
    mechanical work that moves the guider camera (or the fibre head)
    invalidates it — the guider will then faithfully drop stars onto
    a spot that is no longer the fibre.

    If you suspect (or know) the camera was moved:

    1. Illuminate the field with the **flat-field lamp** so the fibre
       hole is visible as a dark spot on a bright background
       (`monitoring`, adjust exp_time until the background is bright
       but unsaturated).
    2. Zoom into the hole region and read off the **pixel coordinates
       of the hole centre** (cursor position readout in the frame
       corner); a screenshot of the zoomed hole helps.
    3. **Send the coordinates + screenshot to the maintainer**
       (address below) — do not edit the service configuration
       yourselves. The maintainer updates `central_point`, and the
       calibrated "home" position of the reticle follows.

    Until the new calibration is applied, the reticle can be dragged
    to the measured hole position manually (right-click) as a
    same-night workaround — but report it regardless, otherwise the
    next observer starts with the stale calibration.

---

## 11. Glossary

- **Service** — the server-side guider process on `services01`; owns
  the camera and mount interaction; holds the operating mode.
- **Client** — a browser session of the web UI; displays service
  state and sends commands; has no state of its own worth preserving.
- **Method** — the solver strategy: `single` (default; holds one star
  selected by the operator) or `fiber` (photocentroid around the
  reticle — currently disabled, §5).
- **Mode** — the service's operating state: `off` (camera idle),
  `monitoring` (frames + solver, **no pulses**), `guiding` (active
  corrections).
- **Reticle** — fixed marker at the fibre entrance (`central_point`).
- **Anchor** — the pixel position the loop holds the star at. Created
  on selection; moved onto the reticle by `drop → reticle`.
- **Acquired** — the solver confirms a guide signal at the expected
  position. Required for `drop → reticle`.
- **Pulse-guide** — short mount correction (`N`/`S`/`E`/`W`,
  ≤ 1500 ms).
- **Re-acquire** (++r++) — force a wide-search around the target after
  a bump, cloud, or lost lock.
- **Wide search** — the circle around the reticle within which stars
  can be selected and re-acquired (*wide search r* slider; config
  default 600 px).

---

*Maintainer:* `mkaluszynski@akond.com`. Service code:
`ocabox-tcs/src/ocabox_tcs/services/guiding_svc/`. Config:
`config/guider.jk15.guider_beso.yaml`.
