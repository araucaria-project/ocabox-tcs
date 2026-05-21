# BESO Guider — Observer Quick Reference

*Appendix to the BESO Camera Interface document (Kimeswenger, v2.30).
This sheet covers operating the autonomous guider that locks the target
on the BESO fibre — a separate service running on `services01.oca.lan` or `tcs.oca.lan`, fed by
the dedicated guider camera on `jk15-ccd.oca.net` (or any other guider if configured). It does **not** cover
the spectrograph itself.*

---

## 1. What it is

A server-side service that reads frames from the guider CCD over
ASCOM/Alpaca, locates the guide signal in each frame, computes the
offset from a chosen target pixel, and issues `PulseGuide` corrections
to the ASA mount through `tic`. The operator interacts only through
the web UI — nothing actually *runs* in the browser, the browser only
displays and dispatches commands.

The guider doesn't know about your science target — it only knows
about pixels on the guider camera. What it does with those pixels
depends on which **method** you pick (§4).

---

## 2. URLs

| Resource      | URL                               |
|---------------|-----------------------------------|
| Guider web UI | `http://services01.oca.lan:8090/` |
| (IP fallback) | `http://192.168.7.45:8090/`       |
| in future     | `http://tcs.oca.lan:8090/`        |

The link is also avalable from the OCM landing page: `http://home.oca.lan/` → "BESO Guider".

---

## 3. Start-of-observations checklist

1. Tertiary at **position ADR06** (BESO path), cover open, dome open,
   target slewed and tracking.
2. Open the guider UI → confirm in the top bar:
   - green NATS dot;
   - the `jk15 / guider_beso` row exists, status `OK`;
   - frame view showing live thumbnails (≈ 2 Hz).
3. Pipeline boots in mode **`monitoring`** — frames flow, solver runs,
   drift is plotted, **no pulses sent**. Safe default for inspection.

---

## 4. Pick a method

Top row of the toolbar — three buttons. **Default is `single`** and
that is what you should start every session with — it's the proven
path for getting a star onto the fibre. `fiber` is a more sophisticated
loop that we'll move to once the star is already on the fibre and we
want photocentroid tracking; treat it as an opt-in upgrade rather than
a normal starting state.

- **`single`** *(default — use this to acquire and to centre on the
  fibre)* — the solver detects discrete star candidates in the frame
  and you pick one. The click point's refined centroid becomes the
  hold target (anchor). The natural workflow:
  1. pick the target star;
  2. switch to `guiding`;
  3. use `drop → reticle` to slide that star onto the fibre.
  After step 3 the star is on the fibre and the loop continues holding
  it there. For most observing sessions this is all you need.

- **`fiber`** *(experimental — switch only after the star is already
  on the fibre)* — the solver computes a photocentroid of all the
  light in a window around the reticle and pulls light back to the
  fibre frame by frame. No "choose a star" step. Useful once `single +
  drop → reticle` has parked the star on the fibre and you want the
  loop tracking the integrated PSF instead of one detected peak. Still
  under live validation; don't be the first night that switches to it
  without telling the maintainer.

- **`multi`** — not implemented yet.

Switching methods is a single button click and takes effect on the
next frame; the mount is not touched by the switch itself.

---

## 5. Acquire and start guiding

Starting state is `monitoring` + method `single`. This is the normal
acquisition path; the fiber method is an optional upgrade applied
after the star is parked on the fibre (§5b).

### 5a. Single-star method — acquire and (optionally) centre on the fibre

1. **Left-click** on a candidate star in the frame view. The solver
   narrow-searches the click point and the refined centroid becomes
   the **anchor** (the pixel position the loop will hold).
   - Use `tab` / `shift+tab` to step through the next/previous
     candidate without clicking.
   - Press `d` to show the detection overlay if you want to see what
     the solver actually found.
2. Switch mode to **`guiding`**. The star is now held at the click
   point; the mount receives `PulseGuide` corrections each frame.
3. **`drop → reticle`** — one-shot command that slides the locked star
   onto the reticle (= fibre entrance) while continuing to guide. Use
   this once the loop is stable and you want spectrograph light. The
   button is enabled only in `guiding` + `acquired`.

After step 3 the star is on the fibre and `single` keeps it there.
For ordinary observing nights this is the whole story.

### 5b. Fiber method — switch *after* the star is on the fibre

Optional upgrade for sessions where you want the loop tracking the
photocentroid of the integrated light instead of one detected peak.
Currently under live validation — don't switch silently.

1. Get the star onto the fibre first using §5a steps 1–3.
2. Click the **`fiber`** method button. The next frame switches the
   solver: candidate detection turns off, the analysis window around
   the reticle takes over, and the hold target becomes the reticle
   itself (not the previous anchor). The mount is not touched by the
   method switch.
3. Stay in `guiding`. The loop now pulls the integrated light back to
   the reticle every frame. There is no `drop → reticle` in fiber mode
   — the reticle *is* the target.

### Sanity check (either method)

Steady-state drift RMS should settle to **≤ 1–2 px** on each axis
within ~30 s. If it grows monotonically, switch back to `monitoring`
and call the maintainer — the pulse-guide Jacobian likely needs
recalibration (it's a per-mount, per-camera-orientation property).

### Keyboard (when the UI window has focus, not a text input)

| Key | Action |
| --- | --- |
| `g` / `m` / `o` | mode → guiding / monitoring / off |
| `r` | re-acquire — force wide-search around target |
| `tab` / `shift+tab` | (single only) cycle lock through detected candidates |
| `h` | reticle home — restore reticle to calibrated default (admin; rare) |
| `↑ ↓ ← →` | manual pulse in image-axis pixels (current step size) |
| `1` / `2` / `3` / `4` | pulse duration → 200 / 500 / 1000 / 2000 ms |
| `+` / `-` / `0` | frame zoom |
| `d` | toggle detection-candidates overlay |
| `?` | full shortcut panel |

### Mouse

- **Left-click** on the frame *(single mode)* → lock onto a star near
  the click. Refines to peak via narrow-search. **Mount is not touched
  until you switch to `guiding`.**
- **Right-click** → move the target reticle (admin op; forces
  wide-search). Don't, unless you know why.
- **Wheel** → zoom around cursor.

---

## 6. ⚠ Do **not** share the guider camera

The guider holds an exclusive ASCOM/Alpaca session on the
`jk15.guider_beso` camera. Pointing **any** other Alpaca/ASCOM client at
the same camera while the guider is running will collide:

- frames stutter, freeze, or arrive corrupted;
- the underlying driver can fall into the state
  `Object reference not set to an instance of an object` and refuse
  every subsequent client until the camera is restarted
  (`camera_restart@jk15-beso`, §5.6 of the main interface document);
- the guider log fills with `OCABOX error 2002` warnings.

**Rule:** during a science observation, the **only** live consumer of
the guider camera is the guider service.

If you need a hand-driven preview (focus check, smoke test, dark
acquisition) **stop the guider first** (see §7), do your work in
MaxIm/SharpCap/ASCOM Diag, then restart the guider.

---

## 7. ⚠ Stopping the guider at end of night

The guider runs on `services01`. **Closing the browser does not stop
it.** Untreated, it will keep imaging and pulsing the mount through
sunrise.

To stop properly:

1. In the UI, set the pipeline mode back to **`monitoring`** (no more
   pulses go to the mount).
2. Then switch to **`off`** — camera capture stops, the service idles.
3. Confirm the mode pill reads `off` and no new thumbnails arrive.

If the UI is unreachable but you need to be sure pulsing has stopped,
SSH to `services01` and either:

```bash
nats --server nats://nats.oca.lan:4222 request \
     'svc.rpc.guider.jk15-guider_beso.v1.set_mode' \
     '{"data": {"mode": "off"}}'
```

or, as a last resort, `sudo systemctl stop tcsd@services01-jk15`.

---

## 8. ⚠ Mount control during guiding

While the pipeline is in `guiding`, the service is talking to the mount
~once per second. **Do not issue manual slews, hand-paddle jogs, or
SyncToCoordinates from another tool**: those compete with the pulses
and the result is a runaway oscillation. To move the telescope, drop
back to `monitoring` first.

The guider only ever issues `PulseGuide` of ≤ 1500 ms. It never slews,
parks, or syncs.

---

## 9. Quick troubleshooting

| Symptom                                   | First thing to check                                            |
| ----------------------------------------- | --------------------------------------------------------------- |
| No thumbnails in UI                       | Thumbnail HTTP base in Connection panel (tunnel host/port).     |
| `drop → reticle` button is greyed out     | Needs `single` + `guiding` + `acquired = yes`. Lock the star first (left-click), then switch to guiding. In fiber mode the reticle *is* the anchor — `drop → reticle` is a no-op and stays disabled. |
| No detection candidates                   | exp_time too low / field too sparse, or you're in `fiber` (which has no candidates by design) — switch back to `single` to acquire. |
| Star locked but drift grows monotonically | Likely Jacobian sign / scale — drop to `monitoring`, call the maintainer. |
| `OCABOX error 2002` in service log        | Gain or binning rejected by driver — config out of range.       |
| Camera marked `unavailable` / NRE         | `camera_restart@jk15-beso`, then restart the guider service.    |
| `WARN AlpacaProtocol: driver rejected …`  | Soft-failed setting, guider continues at the driver's current value — usually harmless. |

---

## 10. Glossary

- **Method** — the solver strategy. `single` (default) locks one
  detected star at the click point; `fiber` (experimental, opt-in)
  integrates light around the reticle once the star is already on the
  fibre. Picked in the toolbar's top row. Switching the method changes
  the *meaning* of every other control, so it sits above mode.
- **Mode** — the pipeline's intent: `off` (camera idle), `monitoring`
  (frames + solver, **no pulses**), `guiding` (active corrections).
- **Reticle** — fixed pixel marker showing the target position
  (`central_point` in config). For BESO it's the calibrated fibre
  entrance. Always drawn; in fiber mode it is *also* the hold target.
- **Anchor** *(single mode only)* — the pixel position the loop holds
  the star at. Created on lock (click / `tab`). Independent of the
  reticle until you use `drop → reticle`.
- **Acquired** — solver-confirmed: a guide signal exists at the
  expected location. Shown in the diagnostics panel; required for
  `drop → reticle` and as an indicator that `guiding` will produce
  meaningful pulses.
- **Pulse-guide** — short mount slew pulse (`N`/`S`/`E`/`W`,
  ≤ 1500 ms) issued by the guider to cancel measured drift.
- **Re-acquire** (`r`) — force a wide-search around the target. Use
  after a mount bump, cloud passage, or anything that lost the lock.
- **Drop → reticle** — one-shot command (single + guiding only): pull
  the currently-locked star onto the reticle while keeping guiding
  active. Equivalent to "centre on fibre after I locked elsewhere".
- **Wide search** — 600 px circle the solver scans when re-acquiring
  (set by `wide_search_radius_px`).

---

*Maintainer:* `mkaluszynski@akond.com`. Service code:
`ocabox-tcs/src/ocabox_tcs/services/guiding_svc/`. Config file in use:
`config/guider.jk15.guider_beso.yaml`.
