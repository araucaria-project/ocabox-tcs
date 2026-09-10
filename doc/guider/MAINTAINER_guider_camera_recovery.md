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

## Recovery — what we now KNOW (tested 2026-07-30)

**There is no gentle path out of this wedge.** Escalation ladder with
verdicts:

1. ~~`PUT connected=false` → `connected=true` on Camera[1] only~~ —
   **DO NOT USE on a wedged camera.** Tested: disconnect OK; reconnect
   blocks the whole RS for ~40 s (USB claim); the **first
   `startexposure` after reconnect crashed the entire Remote Server**,
   taking the Andor down with it. Device-scoped in API, process-wide
   in blast radius.
2. **Restart the ASCOM Remote Server on jk15-ccd** (RDP, restart the
   RS application) — the actual remedy. Plan it: the Andor drops off
   Alpaca for the duration; immediately after restart verify
   `camera/0/ccdtemperature`, `cooleron`, `setccdtemperature` (−60)
   and re-apply the setpoint if the driver came back with cooling off.
3. If the wedge survives an RS restart: USB re-enumeration / host
   reboot at a service window; consider removing the phantom
   Camera[2] profile while there.

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
