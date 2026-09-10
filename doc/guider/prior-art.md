# Prior art — Mirek's guiding attempts and reusable tooling

Two prior implementations live in the repo / sibling branches, plus a body of
helpers from sibling projects. Summary of what's there, what to take, what to
leave, and what we were missing in [architecture.md](architecture.md).

## What exists

### A. `services/guiding_svc/guider_ofp.py` (master, 515 lines)

Direct port of the **OFP** (`oca-fits-proc`) `Guider` module to the TCS repo.
Inherits `fits_proc.modules.abstract_module.AbstractModule`. Drives a per-RPC,
per-FITS pipeline through `FitsManager.process_fits` state store.

Module hierarchy:
```
Guider (dispatcher)              # by rpc.data['request']
├── GuidSimple   ← centroid + ADU-tolerance match, single guide star
├── GuidCalib    ← GuidSimple with 2× search region (initial lock)
├── GuidDark     ← record N raw darks, compute master dark
├── GuidPreview  ← one-shot snapshot for UI
├── GuidStack    ← stub (planned)
└── GuidDiff     ← stub (planned)
```

Per-request flow (per `BaseGuid.run`):
1. Pull image array from `http_conn` (ocabox API call to the camera)
2. Apply master-dark reduction via `ImagesStacking`
3. Either find new star (`find_stars` + `guid_star_selection` by ADU window) or
   re-acquire in subraster around previous star (`calc_correction`, ADU-tolerance
   match within `search_reg_px` window)
4. Save FITS + JPEG thumbnail (with annotated rectangle), publish RPC response
   + journal entry

State persistence: `fm.process_fits.set_op_attr(fits_id, op_id, 'data', ...)` —
the previous guide datum is fetched from the FitsManager store keyed by the
prior fits_id.

### B. `mirk_dev:src/ocabox_tcs/services/guiding_svc/guiding.py` (68 lines)

Mirek's *new-framework* skeleton. **Empty husk:** copy of `dome_follower.py`
that imports `Manager` from `dome_follower_svc.manager` and calls
`manager.guiding()` (which doesn't exist — would crash). Useful only as
confirmation of the template Mirek would start from. Not on master.

### C. The dome-follower service pattern (`services/dome_follower_svc/`)

Mirek's working TCS-native service — the shape he reused for the guiding stub.

```
dome_follower.py    @service       # thin entry: on_start, run_service, on_stop
manager.py          Manager        # owns state and business logic (224 lines)
nats_conn.py        NatsConn       # RPC responders + journal pub (112 lines)
tic_conn.py         TicConn        # ocaboxapi Observatory/Telescope wrapper (46 lines)
```

Notable conventions:
- **Service entry is thin** (~70 lines), `Manager` does the work
- `Manager` holds a back-reference `self.service` to access `monitor` / logger
- `NatsConn` registers RPC responders manually with hardcoded subject patterns
  (`tic.rpc.<tel>.dome.follower.{on,off,state}`)
- `TicConn.init_peripherals(telescope_id)` builds `Observatory`/`Telescope`/
  `Dome`/`Mount` from `ocaboxapi`
- Uses `single_read('tic.config.observatory')` to fetch telescope-wide config
  (mount type, dome radius, geo location) from JetStream
- Uses `self.service.monitor.track_task('checking')` / `set_status(Status.X)`
  for TCS-native monitoring integration

## Can we use it as a start point?

**`guider_ofp.py`** — **No**, not as scaffolding. It's the wrong shape:

- Per-RPC, one-shot module model conflicts with our continuous-pipeline design
  (Collector → Stacker → Solver → Enforcer with bounded queues)
- Pulls in `AbstractModule` from `fits_proc` — wrong base class for TCS
- State lives in `FitsManager.process_fits` — wrong owner; we want pipeline-local
  state inside the Solver
- No notion of "monitoring vs guiding" mode; everything triggers per-RPC

**…but it has substantial _reusable content_** — see below. Treat it as a
**reference implementation of algorithms**, not as scaffolding.

**`guiding.py` (mirk_dev)** — **No**, it's an empty husk.

**Dome-follower pattern** — **Partially yes**. The three-file split
(`<service>.py` thin, `manager.py` heavy, `nats_conn.py` + `tic_conn.py`) is
sound at the *service-entry* level. We adopt it for the **outer shell** of the
guider service:

- `guiding_svc/guider.py` — `@service` entry, builds `Guider × N` and runs
- `guiding_svc/tic_conn.py` — adapt to camera + mount (skip dome)
- `guiding_svc/nats_conn.py` — RPC responder + journal helper (with our
  subject-prefix machinery instead of hardcoded `tic.rpc...`)

The pattern stops being sufficient once we descend into per-pipeline stages —
those need our queue/backpressure model from architecture.md §3.

## What to take from `guider_ofp.py`

### Algorithms (port the math, not the code)

1. **Star finding** — `pyaraucaria.ffs.FFS.find_stars(threshold, kernel_size, fwhm)`.
   Same library, just import directly into our Solver method.
2. **Initial guide-star selection** (`BaseGuid.guid_star_selection`) — match
   detected stars to operator-supplied `star_select` position, falling back to
   first star within `[min_adu, max_adu]`. Solid heuristic; port verbatim.
3. **Subraster re-acquisition** (`BaseGuid.calc_correction`) — given previous
   star position and ADU, look in `(prev ± search_reg_px)` window for a star
   within `(prev_adu ± adu_tolerance)`. Reject ambiguous matches (more than
   one candidate). Port verbatim — this is the working guider algorithm.
4. **Dark cache strategy** — filename keyed by `(exp_time_int, exp_time_decimal,
   loop, nloops)`, master dark by `(exp_time, nloops)`. Port the keying.
5. **Master-dark "ok" status flag** propagated alongside corrections. Add to
   `Correction` payload.

### Library reuse

| What                                          | Source                                      | Use in guider                         |
|-----------------------------------------------|---------------------------------------------|---------------------------------------|
| `FFS.find_stars`                              | `pyaraucaria.ffs`                           | Solver methods (`simple`, `centroid`) |
| `save_fits_from_array`                        | `pyaraucaria.fits`                          | `save_raw_fits` / `save_stacked_fits` |
| `ImagesStacking`                              | `fits_proc.images_stacking`                 | Stacker stage + master dark builder   |
| `AstroTools.image_stretch_display`            | `fits_proc.astro_tools`                     | Thumbnail generation                  |
| `Folders.folder_processed(tel_id, 'guiding')` | `fits_proc.folders`                         | Output path convention                |
| `AsyncListIter` / `AsyncRangeIter`            | `fits_proc.iter_async`                      | CPU loops that mustn't block heartbeat|
| `OpenCV` (`cv2.rectangle`, `cv2.cvtColor`)    | system                                      | Annotated thumbnails                  |
| `scipy.signal.convolve2d`                     | system                                      | Optional Gaussian smoothing kernel    |
| `Observatory`/`Telescope`/`Mount` (ocaboxapi) | `ocaboxapi`                                 | TIC connection (camera + mount)       |
| `single_read('tic.config.observatory')`       | serverish + tic                             | Pixel scale, mount type, geo config   |
| `MsgJournalPublisher` / `journ_pub`           | `serverish.messenger`                       | Operator-facing journal              |

### Dependency strategy decision

`oca-fits-proc` (`fits_proc.*`) is a heavy dep — it pulls in dynaconf, full
modules tree, OFP framework. Resolution after B/C/D rounds:

**Outcome — pyaraucaria with optional extras**:

- **All extracted code → `pyaraucaria`**, including protocol clients (Alpaca
  imagebytes etc.). Final consideration: `ocaboxapi` wraps TIC and doesn't
  speak HTTP/Alpaca, so it's the wrong home for image-fetch protocols.
  pyaraucaria is already a "miscellaneous low-level astro utilities" bucket
  (FFS, fits, dome_eq) — adding more fits naturally.
- **New transitive deps** (e.g., `aiohttp` for Alpaca) gated behind
  optional extras: `pip install pyaraucaria[alpaca]`. Default install stays
  lean for users who only need FFS/fits/dome_eq.
- **OFP-specific** (path scheme, downloader, modules) → stay in OFP.
- **Drop entirely**: `iter_async`, `utils` (re-implementations of stdlib).

TCS guider depends on `pyaraucaria[alpaca]` (real Python dep) and
`ocaboxapi` (for TIC handles — Mount, Telescope), and relates to
`oca-fits-proc` only as a NATS RPC peer (`DownloaderRPCBackend`, no Python
import).

Concrete extraction plan and per-piece effort lives in
[packaging-plan.md](packaging-plan.md). Coordination required with Mirek
(OFP cutover) and Mikołaj (pyaraucaria + ocaboxapi PRs); TCS guider
scaffolding does not block on these.

`pyaraucaria` is already a transitive dep (used by ocaboxapi/serverish stack);
free.

## Ideas not currently in [architecture.md](architecture.md)

These came out of Mirek's prior art and should be folded in:

1. **Master-dark management workflow** — explicit `dark` request type that
   records N exposures and stacks into a master. Architecture currently mentions
   "darks" only in passing. Needs: who triggers (operator command? automatic at
   sunset?), where stored, lifetime, how the Stacker discovers it.
2. **Calibration mode (`calib`)** — initial star lock with wider search region
   (e.g. 2× normal). Should be an explicit pipeline mode or a transient state
   inside `monitoring`/`guiding`. Currently architecture has `mode ∈ {off,
   monitoring, guiding}` only.
3. **Preview / snapshot** — was a `❓ snapshot` open question; Mirek confirms
   need (UI wants one-shot frame). Promote to definite command.
4. **Star-lost event** — explicit named event when previous star can't be
   re-acquired in the subraster. Add to telemetry events list.
5. **`cam_sim` backend for Collector** — switch to a simulator camera that
   serves frames over HTTP. Add to Collector backend list as concrete variant
   (not just generic "direct download").
6. **Operator journal channel** — separate from telemetry, human-readable
   messages via `MsgJournalPublisher`. Architecture mentions "events" but
   conflates structured telemetry with operator messages. Split into:
   - `…events` — structured (machine consumers)
   - `…journal` — text (operator UI / Halina AI)
7. **ADU-tolerance star matching** — re-acquisition uses ADU window not just
   position; this is the actual robustness mechanism. Should appear in Solver
   method spec, not be implicit.
8. **Solver method enumeration** — Mirek had `simple`, planned `stack`, planned
   `diff`. Architecture currently says "method (with parameters - method
   specific)" abstractly; concrete catalogue needed:
   - `simple` — centroid + ADU match (Mirek's `GuidSimple`)
   - `stack` — multi-frame solver (planned, weighted stacking before centroid)
   - `diff` — image differencing (planned, for crowded fields)
   - `cross_correlation` — referenced in architecture, not in OFP
   - `dummy` — for integration phase
9. **Path / output convention** — `<processed>/<tel_id>/guiding/<...>` rooted
   via `Folders`. Should be in config and consistent across cameras.
10. **Telescope-config fetch** — `single_read('tic.config.observatory')` is
    the canonical source for pixel scale, mount type, geo location. CameraInfo
    should be **derived from** this rather than declared separately.

## Tooling Mirek uses we should adopt

Already covered above (lib reuse table). Highlights:

- **`single_read` for retained config** — JetStream as durable config store
  (vs YAML-only). Useful for things that change at runtime (pixel scale after
  binning change).
- **`MsgJournalPublisher`** — for operator-readable channel separate from
  telemetry. We weren't using it; should be.
- **`monitor.track_task('label')`** — already TCS-native; use for each stage's
  per-frame work (`'collect'`, `'stack'`, `'solve'`, `'enforce'`) so `tcsctl`
  shows what the guider is doing.
- **`AsyncListIter`/`AsyncRangeIter`** — keeps async loop responsive when
  iterating large arrays. We'll need this in Solver for big frames.

## Concrete next moves before scaffolding

Folding this into the architecture document:

1. Add §12 "Reuse from prior art" pointing to this doc and the dependency-strategy decision
2. Update §3.4 (Enforcer) and §4 (Controller) to mention dark/calib/preview commands
3. Update §3.3 (Solver) with concrete method catalogue
4. Replace the bare "events" channel in §4.3 with `events` + `journal` split
5. Update §5 NATS subject map to include `…journal` subject
6. Add to §11 decisions: Option A vs Option B for dependency strategy
