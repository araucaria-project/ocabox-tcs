# Draft reply to Stefan — 2026-07-30

Subject: Re: guider session 29/30 July — analysis, confirmed bugs, fixes

Hi Stefan,

thank you — genuinely. The videos and screenshots are the most useful
guider feedback we have received so far. I went through them frame by
frame against the service journal from that night, and your session
uncovered two real code bugs, one serious safety gap, and several
documentation errors. Point by point:

**1. Zoom scales the overlay lines and text — confirmed.**
Plain UI defect: the overlays are drawn in image space, so zoom
thickens them until they cover the star you're trying to see. Fix is
underway — stroke widths and labels will stay constant on screen
regardless of zoom.

**2. "It centers on the brightest spot aside, or even a dark minimum" —
confirmed, and it was worse than a tuning issue.** Two distinct bugs in
the `fiber` method, both visible in your videos:

- In `fiber` + `guiding`, the correction sign was inverted relative to
  the calibrated mount model. The loop actively pushed the star *away*
  from the fiber — your first video (22:56–22:57 UT) shows the textbook
  divergence, error growing from 0.3 to 12 px in ~11 s. Not a
  calibration problem; a sign error, now identified precisely.
- The `fiber` "acquired" detector could pass on pure background noise
  (a statistics bug in the detection threshold). That is the green
  circle you saw wandering over empty sky reporting a few ADU — it had
  long lost the star and nobody told you. Also identified precisely.

Both are being fixed, and until the fixes are validated on sky,
**please treat `fiber` as off-limits** — the manual now says so
explicitly. The good news: in your second video, once you switched back
to `single`, the loop correctly acquired and held your real star. What
it did *not* do is move it to the fiber — and that is by design, which
the documentation explained too quietly: `guiding` **holds** the star
where it was locked; putting it **onto the fiber** is the separate
`drop → reticle` button (enabled in `single` + `guiding` + acquired).
Workflow for your next run: click the star → `guiding` → `drop →
reticle`. That's the whole story; `fiber` was never required.

About the arrow keys overshooting even at step "1": also confirmed and
quantified. The keys 1–4 select pulse *durations* (200–2000 ms), and on
jk15 the calibrated response is ~26 ms per pixel — so the smallest
keyboard pulse moves ~8 px. Factor of ~10 too big for centering on a
5 px fiber hole, exactly as you wrote. We will re-denominate the manual
pulse controls in pixels (with a 1 px minimum) and fix the panel layout
so the manual-pulse buttons stay visible in fiber mode.

**3. Stopping procedures failed — your report was correct and the fault
was ours, in the manual itself.** The quick-reference told you to use a
`nats` CLI that is not installed on services01, and a systemd unit name
that does not exist — instructions that were never tested on the target
host. I apologise — that cost you time at 1 a.m. That whole "SSH last
resort" section is now **removed**, deliberately: the supported way to
stop the guider is the UI — `monitoring`, then `off`, then **verify**
the mode pill actually reads `off` and thumbnails stop. If the UI does
not respond, check the connection indicator and reload the page; if it
still won't respond, that is a maintainer problem, not something an
observer should have to fix over SSH at night. The safety changes under
point 4 are what make this policy sound — the service is becoming
responsible for never running away in the first place.

**4. "Service still works somewhere in nowhere and miscontrols the
telescope" — confirmed, and this is the serious one.** The journal
shows what happened: at 23:25 UT the guider camera's frame buffer froze
(the driver kept serving the same image — likely fallout of the camera
contention described in §6 of the quick reference, possibly combined
with a firmware quirk). The guider saw the "star" at a constant 18 px
offset, and re-issued the identical correction pulse (N 230 ms +
E 54 ms) every ~5 seconds — for the next 10.5 hours, until it was
stopped at 09:49 UT. That is what fought your manual guiding attempts
and streaked the stars on your Andor frames. Your OFF clicks never
reached the service: your UI session had disconnected (the NATS address
in your screenshots reads `ws://192.168.38:9222` — an octet went
missing; it must be `ws://192.168.7.38:9222`), and the UI accepted the
clicks silently instead of telling you it was dead. Multiple failures
lined up, and none of the safety nets that should have existed did.

Consequences, all in progress:
- duplicate-frame detection — a frozen camera can no longer produce
  corrections;
- a guiding watchdog — if the correction stops changing, or the star is
  not genuinely re-acquired for some minutes, the service demotes
  itself to `monitoring` and raises an alarm instead of pulsing on;
- repeated mount errors (the pulse storm your log shows at 05:20 UT)
  now latch the service out of guiding instead of being retried
  forever;
- your ADR6 suggestion is adopted, and extended: the guider will
  switch itself **off** when the tertiary mirror leaves the BESO
  position (it has physically lost its light feed — nothing to guide),
  and will drop out of guiding automatically when the mount starts a
  commanded slew, exactly as you proposed;
- the UI gets an unmissable "DISCONNECTED" state and will refuse (not
  swallow) commands while the link is down.

Once the safety fixes and the fiber corrections are deployed, I'll send
a short note with what changed and an updated one-page workflow. If you
have a night available after that, a repeat of your test — same
ruthless honesty — would be extremely valuable.

Thanks again. This is exactly the feedback that makes the tool usable.

Cheers,
Mikołaj

---
*(internal note, not part of the email: full analysis in
`doc/guider/NIGHT_REPORT_2026-07-29_stefan.md`)*
