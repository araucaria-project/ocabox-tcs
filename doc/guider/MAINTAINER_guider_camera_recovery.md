# MAINTAINER — jk15 guider-camera recovery (jk15-ccd Alpaca)

*Maintainer-only. Observers: the observer manual deliberately says
"contact the maintainer" — none of the procedures below are
observer-safe. Findings from the 2026-07-30 daytime investigation
(after the 2026-07-29 incident, `NIGHT_REPORT_2026-07-29_stefan.md`).*

## Topology (verified live 2026-07-30)

One **ASCOM Remote Server** (P. Simpson, 6.7.1) per Windows host;
device-scoped, but **one process** — killing it takes down every
device it serves.

| Host | Device | Hardware | Role |
|---|---|---|---|
| jk15-ccd | Camera[0] | Andor iKon-XL 230 (`XL230A_84,BV`) | **science imager — cooled, do not disturb** |
| jk15-ccd | Camera[1] | ZWO ASI174MM Mini | **BESO guider** (tcs `guider_beso`) |
| jk15-ccd | Camera[2] | "ZWO ASI174MM Mini" | **phantom** — connected=True but exposures return all-zero frames (no effective hardware claim). Origin unknown; suspected destabiliser of Camera[1]'s USB claim. Candidate for removal from the RS profile at next service window. |
| jk15-tcu | Camera[0] | ASCOM Dynamic Driver → bridge to ccd Camera[0] | Andor via Alpaca-in-Alpaca |
| jk15-tcu | Camera[1] | ZWO ASI174MM Mini (gain 240) | Andor-branch guider |
| jk15-tcu | others | Telescope (Autoslew), Dome, Rotator, Focuser, FilterWheel, CoverCalibrator | **mount/dome control path** |

Do not confuse with host `jk15-beso` (BESO **spectrograph science
camera**, ssh-alias managed — Kimeswenger doc §5). Nothing here
applies to it.

## Failure mode: frozen imagearray ("live-looking wedge")

Signature (all four at once):
- `startexposure` accepted, `ErrorNumber=0`;
- `camerastate` genuinely cycles Idle→Exposing→Idle (~5.5 s regardless
  of requested duration);
- `imageready` never asserts (this part is chronic on the ZWO
  instances even when healthy);
- `imagearray` returns **byte-identical pixels regardless of exposure
  time** — the definitive test: two exposures 0.1 s vs 2.0 s, compare
  content; identical ⇒ wedged. Command-path health checks all pass —
  only content comparison catches it.

Guider-side protection (v1.3.3+): `FrameDeduplicator` drops the stale
frames, logs `Camera appears FROZEN`, solver starves, no pulses.

## Recovery — escalation ladder (verdicts from live incidents)

1. **USB port restart + RS reload (RDP on jk15-ccd)** — the gentlest
   sequence that has actually cleared the wedge (verified 2026-08-04:
   `imageready` path and exposure timing fully restored, no host
   reboot). Steps: in *USB Device Tree Viewer* restart the port (or
   restart the device) hosting the ASI, then restart the ASCOM Remote
   Server application. A port restart **alone** is not enough — after
   it the camera cycled faster but the wedge symptoms remained
   (exposures ~10× nominal, flag never asserting); the RS reload is
   the part that completes the recovery.
   **Cooling hazard**: the sequence reliably comes back with the
   **Andor cooler OFF** — immediately verify `camera/0/ccdtemperature`
   and `cooleron`, and re-apply the setpoint (**−50**, confirmed live:
   equilibrium ≈ −49.8 °C; an earlier note here said −60, wrongly).
   Operator-friendly path from zenith/nadir:
   `oca jk15 camera settemperature -- -50` (monitor with
   `oca jk15 camera temperature --monitor`).
   Note: instead of restarting the RS application, opening **Setup →
   exit** in the RS window reloads all drivers in place — same effect
   and the same Andor-cooling hazard, one less window to manage. The
   observer manual (`troubleshooting.md`, "Guider camera does not
   produce images") documents this flow for observers via DWservice.
2. ~~`PUT connected=false` → `connected=true` on Camera[1] only~~ —
   **DO NOT USE on a wedged camera.** Tested (2026-07-30): disconnect
   OK; reconnect blocks the whole RS for ~40 s (USB claim); the
   **first `startexposure` after reconnect crashed the entire Remote
   Server**, taking the Andor down with it. Device-scoped in API,
   process-wide in blast radius.
3. **Restart the ASCOM Remote Server alone** (without the USB step) —
   remedy for RS-level faults; same Andor-cooling caveat as rung 1.
4. If the wedge survives rungs 1/3: host reboot at a service window;
   consider removing the phantom Camera[2] profile while there.

Quick verdict after any attempt: one probe exposure on Camera[1] and
watch `camerastate`/`imageready` — healthy means state 2 for roughly
the exposure time and `imageready=true` within ~1 s after; wedged means
state 2 stretched to ~10× nominal and the flag never asserting.

Related hazard (2026-07-30): on **jk15-tcu**, a single JSON
`imagearray` GET on Camera[1] **crashed that RS** (mount/dome drivers
down until manual restart). Until understood: never fetch full-frame
`imagearray` as JSON from the tcu server — binary `imagebytes` only,
and test on a small ROI first.

## Diagnostic one-liners (read-only, safe)

```bash
# device inventory
curl -s http://jk15-ccd.oca.lan:11111/management/v1/configureddevices | jq .
# frozen-buffer test (run twice with different Duration, compare)
curl -s -X PUT http://jk15-ccd.oca.lan:11111/api/v1/camera/1/startexposure \
     -d 'Duration=0.3&Light=true&ClientID=990'
sleep 4
curl -s 'http://jk15-ccd.oca.lan:11111/api/v1/camera/1/imagearray?ClientID=990' | md5sum
# NOTE: md5 over raw JSON differs on transaction IDs — for a strict
# comparison hash the pixel Value only (see /tmp/rest_test.py pattern).
# Andor health
curl -s 'http://jk15-ccd.oca.lan:11111/api/v1/camera/0/ccdtemperature?ClientID=990'
```

## Open items

- [ ] Identify origin of Camera[2] phantom profile; remove it (service
      window, RDP to jk15-ccd → ASCOM Remote Server settings).
- [ ] Report to camera-server owner: `imageready` never asserts on ZWO
      instances; RS crashes on tcu JSON imagearray; RS crash on
      post-reconnect exposure.
- [ ] P0.5 in tcs guider: invalidate `acquired` / publish DEGRADED
      after N consecutive duplicate frames (UI must show the freeze,
      not a stale healthy-looking lock).
