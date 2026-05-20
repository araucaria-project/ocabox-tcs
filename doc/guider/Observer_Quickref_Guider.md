# BESO Guider — Observer Quick Reference

*Appendix to the BESO Camera Interface document (Kimeswenger, v2.30).
This sheet covers operating the autonomous guider that locks the target
on the BESO fibre — a separate service running on `services01.oca.lan` or `tcs.oca.lan`, fed by
the dedicated guider camera on `jk15-ccd.oca.net` (or any other guider if configured). It does **not** cover
the spectrograph itself.*

---

## 1. What it is

The guider is a server-side service. It reads
frames from the guider CCD via
ASCOM/Alpaca, locates the guiding star in each frame,
computes the offset from a fixed pixel target (ususally previous position), and
issues `PulseGuide` corrections to the ASA mount through `tic`. The
operator interacts only through a web UI; nothing runs in the browser
itself.

---

## 2. URLs

| Resource      | URL                                  |
|---------------| ------------------------------------ |
| Guider web UI | `http://services01.oca.lan:8090/`    |
| (IP fallback) | `http://192.168.7.45:8090/`          |

---

## 3. Start-of-observations checklist

1. Tertiary mirror at **position ADR06** (BESO path), cover open, dome
   open, target slewed and tracking.
2. Open the guider UI → confirm in the top bar:
   - green NATS dot
   - the `jk15 / guider_beso` row exists, status `OK`
   - thumbnail panel showing live frames (≈ 2 Hz)
3. The guider in **`monitoring`** — frames flow: the solver
   runs, drift is plotted, but **no pulses are sent**. This is the safe
   default. Verify a star is visible and reasonably centred on the fibre
   reticle.

---

## 4. Acquiring and locking on the fibre

The fibre entrance is pre-calibrated and stored as `central_point`
(reticle position). Operator does not normally touch it.

1. After your slew, the star may not be on the fibre — drift it in with
   the mount until it sits within the search box around the reticle.
2. Click **Lock** (or press `L`) → the current centroid becomes the
   guide anchor. The drift chart resets to zero.
3. Switch mode to **`guiding`** (mode toolbar, top-left). The pulse
   arrow overlay appears each time a correction is issued.
4. Steady-state drift RMS should be **≤ 1–2 px** on each axis within
   ~30 s. If it grows, switch back to `monitoring` and call the
   maintainer — the pulse-guide Jacobian may need recalibrating.

Useful keys: `L` = lock anchor at current centroid, `H` = home reticle
back to the calibrated fibre position, `Space` = toggle between
`monitoring` and `guiding`.

---

## 5. ⚠ Do **not** share the guider camera

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
acquisition) **stop the guider first** (see §6), do your work in
MaxIm/SharpCap/ASCOM Diag, then restart the guider.

---

## 6. ⚠ Stopping the guider at end of night

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

## 7. ⚠ Mount control during guiding

While the pipeline is in `guiding`, the service is talking to the mount
~once per second. **Do not issue manual slews, hand-paddle jogs, or
SyncToCoordinates from another tool**: those compete with the pulses
and the result is a runaway oscillation. To move the telescope, drop
back to `monitoring` first.

The guider only ever issues `PulseGuide` of ≤ 1500 ms. It never slews,
parks, or syncs.

---

## 8. Quick troubleshooting

| Symptom                                   | First thing to check                                            |
| ----------------------------------------- | --------------------------------------------------------------- |
| No thumbnails in UI                       | Thumbnail HTTP base in Connection panel (tunnel host/port).     |
| `OCABOX error 2002` in service log        | Gain or binning rejected by driver — config out of range.       |
| Camera marked `unavailable` / NRE         | `camera_restart@jk15-beso`, then restart the guider service.    |
| Star locked but drift grows monotonically | Wrong sign in Jacobian — switch to `monitoring`, call maintainer.|
| `WARN AlpacaProtocol: driver rejected …`  | Soft-failed setting, guider continues at the driver's current value — usually harmless. |

---

## 9. Glossary

- **Anchor**: the pixel position the controller corrects toward. By
  default = `central_point` (fibre entrance). `Lock` rewrites it to the
  star's current centroid.
- **Pulse-guide**: short mount slew pulse (`N`/`S`/`E`/`W`, ≤ 1500 ms)
  issued by the guider to cancel measured drift.
- **Reticle**: the on-screen marker showing where the star *should* sit
  — i.e. the current anchor.
- **Wide search**: 600 px search circle used when re-acquiring a lost
  star (after clouds, mount bump, slew).
- **`monitoring` / `guiding` / `off`**: the three pipeline modes —
  watching, correcting, idle.

---

*Maintainer:* `mkaluszynski@akond.com`. Service code:
`ocabox-tcs/src/ocabox_tcs/services/guiding_svc/`. Config file in use:
`config/guider.jk15.guider_beso.yaml`.
