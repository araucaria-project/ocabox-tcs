"""Standalone calibration probe driver — diagnostic edition.

Designed to characterise probe-to-probe scatter on a single bright
star. Runs three experiments back-to-back:

  1. **No-pulse baseline** — sample acquired_pos twice 1.5s apart
     repeatedly. The Δ between samples is pure sidereal drift +
     pipeline measurement noise. Sets a noise floor for what we should
     expect with no pulse at all.

  2. **Short-settle probes** — repeated probes with the default
     ``post_pulse_settle_ms=1000``. Replicates user's session.

  3. **Long-settle probes** — same probes with ``post_pulse_settle_ms=2500``.
     If the per-probe scatter drops significantly, settle was the
     dominant noise source.

Each round prints raw probe data plus median + σ per direction so we
can compare distributions.

Run from the repo root:

    poetry run python scripts/calibration_session.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

from serverish.messenger import Messenger, get_rpcrequester


NATS_HOST = "nats.oca.lan"
NATS_PORT = 4222
RPC_BASE = "svc.rpc.guider.jk15.guider_beso.pipeline.mon.v1"


async def call(cmd: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    req = get_rpcrequester(f"{RPC_BASE}.{cmd}")
    data, _meta = await req.request(payload or {}, timeout=timeout)
    return data


async def snapshot() -> dict[str, Any]:
    """Return the controller-level inner snapshot dict (mode, acquired,
    acquired_pos, etc.). Strips the RPC envelope wrapper."""
    env = await call("snapshot")
    return env.get("result", {})


async def probe(direction: str, duration_ms: int, settle_ms: int) -> dict[str, Any]:
    payload = {
        "direction": direction,
        "duration_ms": duration_ms,
        "post_pulse_settle_ms": settle_ms,
        "timeout_s": 30.0,
    }
    t0 = time.monotonic()
    env = await call("calibrate_probe", payload, timeout=40.0)
    elapsed = time.monotonic() - t0
    inner = env.get("result", {}) if env.get("status") == "ok" else env
    inner["_elapsed_s"] = elapsed
    return inner


def fmt_probe(direction: str, duration_ms: int, r: dict[str, Any]) -> str:
    if r.get("status") != "ok":
        return f"  {direction} {duration_ms}ms  ERR: {r.get('error')!r}  ({r.get('_elapsed_s', 0):.2f}s)"
    pb = r.get("pos_before", [0, 0])
    pa = r.get("pos_after", [0, 0])
    return (
        f"  {direction} {duration_ms:4d}ms  Δ=({r['dx']:+7.2f},{r['dy']:+7.2f})  "
        f"pos {pb[0]:.1f},{pb[1]:.1f} → {pa[0]:.1f},{pa[1]:.1f}  ({r['_elapsed_s']:.2f}s)"
    )


async def baseline_no_pulse(n: int, wait_s: float) -> None:
    print(f"\n=== Baseline: no-pulse, {n} samples spaced {wait_s}s ===")
    deltas = []
    last_pos = None
    last_adu = None
    for i in range(n):
        snap = await snapshot()
        pos = snap.get("acquired_pos")
        adu = snap.get("acquired_adu")
        if pos is None:
            print(f"  [{i}] not acquired — skipping")
            await asyncio.sleep(wait_s)
            continue
        if last_pos is not None:
            dx = pos[0] - last_pos[0]
            dy = pos[1] - last_pos[1]
            deltas.append((dx, dy))
            print(f"  [{i}] pos={pos[0]:.2f},{pos[1]:.2f} adu={adu:.0f}  Δ from last=({dx:+5.2f},{dy:+5.2f})")
        else:
            print(f"  [{i}] pos={pos[0]:.2f},{pos[1]:.2f} adu={adu:.0f}  (start)")
        last_pos = pos
        last_adu = adu
        await asyncio.sleep(wait_s)
    if deltas:
        dxs = [d[0] for d in deltas]
        dys = [d[1] for d in deltas]
        print(
            f"\n  no-pulse drift over {wait_s}s: "
            f"med Δ=({statistics.median(dxs):+5.2f},{statistics.median(dys):+5.2f})  "
            f"σ=({statistics.stdev(dxs) if len(dxs) > 1 else 0:.2f},"
            f"{statistics.stdev(dys) if len(dys) > 1 else 0:.2f})"
        )


async def round_directions(
    label: str,
    duration_ms: int,
    settle_ms: int,
    n_per_dir: int,
    rest_s: float = 1.5,
) -> None:
    print(f"\n=== {label}: {duration_ms}ms pulse, settle={settle_ms}ms, {n_per_dir}× per direction ===")
    rows: dict[str, list[tuple[float, float]]] = {}
    elapsed_by_dir: dict[str, list[float]] = {}
    for direction in ("N", "S", "E", "W"):
        rows[direction] = []
        elapsed_by_dir[direction] = []
        print(f"\n--- {direction} ---")
        for _ in range(n_per_dir):
            r = await probe(direction, duration_ms, settle_ms)
            print(fmt_probe(direction, duration_ms, r))
            if r.get("status") == "ok":
                rows[direction].append((r["dx"], r["dy"]))
                elapsed_by_dir[direction].append(r["_elapsed_s"])
            await asyncio.sleep(rest_s)

    # Backlash-aware summary: report median twice — once including all
    # samples, once dropping the first probe of each direction (which
    # always burns gear backlash and undermeasures motion). The
    # post-drop number is what should go into the Jacobian.
    print(f"\n--- {label} summary ---")
    for direction, vals in rows.items():
        if not vals:
            print(f"  {direction}: no valid samples")
            continue
        dxs = [v[0] for v in vals]
        dys = [v[1] for v in vals]
        es = elapsed_by_dir[direction]
        med_dx = statistics.median(dxs)
        med_dy = statistics.median(dys)
        sd_dx = statistics.stdev(dxs) if len(dxs) > 1 else 0.0
        sd_dy = statistics.stdev(dys) if len(dys) > 1 else 0.0
        print(
            f"  {direction}  n={len(vals)}  "
            f"med Δ=({med_dx:+7.2f},{med_dy:+7.2f})  "
            f"σ=({sd_dx:5.2f},{sd_dy:5.2f})  "
            f"k_med/ms=({med_dx/duration_ms:+.5f},{med_dy/duration_ms:+.5f})  "
            f"elapsed={[f'{e:.1f}' for e in es]}"
        )
        # Drop-first-probe view (only meaningful with ≥2 probes).
        if len(vals) >= 2:
            dxs2 = dxs[1:]
            dys2 = dys[1:]
            md2_x = statistics.median(dxs2)
            md2_y = statistics.median(dys2)
            sd2_x = statistics.stdev(dxs2) if len(dxs2) > 1 else 0.0
            sd2_y = statistics.stdev(dys2) if len(dys2) > 1 else 0.0
            print(
                f"  {direction}  drop-first n={len(dxs2)}  "
                f"med Δ=({md2_x:+7.2f},{md2_y:+7.2f})  "
                f"σ=({sd2_x:5.2f},{sd2_y:5.2f})  "
                f"k_med/ms=({md2_x/duration_ms:+.5f},{md2_y/duration_ms:+.5f})  "
                f"  ← use these for the Jacobian"
            )


async def main() -> None:
    msgr = Messenger()
    await msgr.open(NATS_HOST, NATS_PORT)
    try:
        snap = await snapshot()
        print(
            f"snapshot: mode={snap.get('mode')} acquired={snap.get('acquired')} "
            f"pos={snap.get('acquired_pos')} adu={snap.get('acquired_adu')}"
        )
        if snap.get("mode") != "monitoring":
            print("ERROR: pipeline not in monitoring mode — switch via UI first.")
            return
        if not snap.get("acquired"):
            print("ERROR: no lock — acquire a star via UI first.")
            return

        # Experiment 1 — sidereal-drift baseline (no pulse).
        await baseline_no_pulse(n=6, wait_s=1.5)

        # Experiment 2 — short settle (1000ms), short pulses (500ms),
        # 4 per direction. This is essentially what user just ran.
        await round_directions(
            label="short-settle 500ms",
            duration_ms=500,
            settle_ms=1000,
            n_per_dir=3,
        )

        # Experiment 3 — long settle (2500ms), same pulses.
        # If σ shrinks vs experiment 2, settle was the noise source.
        await round_directions(
            label="long-settle 500ms",
            duration_ms=500,
            settle_ms=2500,
            n_per_dir=3,
        )

        # Experiment 4 — long pulses, long settle. Higher SNR.
        await round_directions(
            label="long pulse 1000ms long-settle",
            duration_ms=1000,
            settle_ms=2500,
            n_per_dir=3,
        )
    finally:
        await msgr.close()


if __name__ == "__main__":
    asyncio.run(main())
