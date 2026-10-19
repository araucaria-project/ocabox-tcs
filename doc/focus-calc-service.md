# focus_calc — NATS focus-calculation service

Status: **boilerplate / API proposal** (PR for discussion). The wire
protocol below is the contract we want to stabilise; the batch methods
already work (via `pyaraucaria`), dynamic search is a stub.

## What it is

A permanent TCS service that computes best-focus from FITS frames, as
**jobs** driven entirely over NATS sub/pub. The service never moves
hardware — the client (TOI, plan-runner, a script) drives the focuser
and tells the service where the frames land.

Design points, in requirements order:

- **N concurrent jobs**, each bound to a frame directory. Frames may
  all exist up front, or trickle in during a scan — every new frame
  triggers a re-evaluation and fresh results ("iterative improvement").
- **M methods per job** on the same frames — one result message per
  method per round. Methods are the existing TOI/pyaraucaria
  algorithms (`rms`, `rms_quad`, `fwhm`, `laplacian`, `lorentzian`);
  the plug-in protocol also anticipates **dynamic-search** methods
  that suggest the next focuser position or convergence
  (`suggestion.kind: next_position | stop | none`).
- **Client-defined scans**: the `open` message may carry a `scan`
  descriptor (informational for now) — the client executes it, we
  refine after each frame.
- **Job closure**: explicit `stop` message, or idle timeout (no new
  input for `idle_timeout_s`).
- **Sub/pub, not RPC** — a job is a long-lived conversation with many
  messages in both directions. (RPC for a fixed, complete image set is
  a possible later convenience wrapper.)

## Subjects

All under a configurable root (default `focus.calc`):

```
IN   focus.calc.job.<id.part1[.part2…]>     commands (open / file / stop)
OUT  focus.calc.result.<effective_id>       one message per method per round
OUT  focus.calc.state.<effective_id>        lifecycle (opened / rejected / closed)
```

**The subject suffix is the job id.** The client picks it (dots
allowed, e.g. `zb08.20260804.autofocus`); all responses use the same
suffix. If the id was already used by a *finished* job, the service
appends `.h<8-hex>` and announces the **effective id** in the
`opened` state message — clients must subscribe to
`focus.calc.result.<effective_id>` (or just `focus.calc.result.<their.id>.>`
plus the exact id, to cover both cases).

Messages to an *active* id join the running job — that is how `file`
and `stop` reach it.

### JetStream

A stream must cover `focus.calc.>` — provision via
[oca_nats_config](https://github.com/araucaria-project/oca_nats_config)
before deploying (same lesson as `tic.command.>`; the guider fails
fast on uncovered publish subjects and this service should follow once
it grows past boilerplate).

## Payloads

`open` (implicit when the first message for a new id carries `path`):

```json
{
  "action": "open",
  "path": "/data/zb08/focus/actual",
  "methods": ["laplacian", "rms_quad"],
  "params": {"focus_keyword": "FOCUS", "crop": 10},
  "idle_timeout_s": 300,
  "scan": {"start": 14000, "stop": 16000, "step": 100}
}
```

`file` — optional explicit notification (folder is also polled every
`poll_interval_s`, so pure file-drop clients work too):

```json
{"action": "file", "filename": "zb08c_0042.fits"}
```

`stop`:

```json
{"action": "stop"}
```

`result` (per method, per round; `fit` mirrors what TOI's focus window
plots today):

```json
{
  "seq": 7,
  "job_id": "zb08.20260804",
  "effective_id": "zb08.20260804",
  "method": "laplacian",
  "status": "ok",                    // partial | ok | failed
  "best_focus": 15230.0,
  "files_used": 9,
  "suggestion": {"kind": "none", "position": null},
  "fit": {"coef": [...], "focus_values": [...], "sharpness_values": [...],
          "fit_x": [...], "fit_y": [...]},
  "message": ""
}
```

`state` events: `opened` (carries `effective_id`, echo of parameters),
`rejected` (reason: job limit, missing path), `closed` (reason: stop /
idle / service stopping, plus `final` — last result of every method).

## Service configuration

```yaml
services:
  - type: focus_calc_svc.focus_calc
    instance_context: main
    subject_root: focus.calc
    default_methods: [laplacian]
    default_idle_timeout_s: 300
    poll_interval_s: 2.0
    max_jobs: 16
```

Dependencies: `pip install ocabox-tcs[focus]` (pulls `pyaraucaria`).
Without the extra the service boots and honestly reports batch methods
as unavailable (per-method `failed` results + discovery metrics).

## Observability

Standard TCS monitoring (status, heartbeat, `tcsctl`). Metrics
(`tcsctl --detailed`): method availability map, active job table
(path, files, rounds, methods), message/result counters, last error.
Modelling jobs as TCS child components (hierarchical monitoring) is a
noted option once multi-component services land (roadmap Feature 3).

## Open questions for review

1. Subject root — `focus.calc` vs something under an existing family
   (`svc.focus.…`? telescope-scoped roots?).
2. Should `scan` be enforced (service checks incoming frames against
   the declared plan) or stay informational?
3. RPC wrapper for the complete-set case — worth it, or does a
   one-shot job (open + immediate stop after last file) suffice?
4. Result retention: JetStream limits for `focus.calc.result.>`
   (suggested: days, like `svc_status`).
5. Per-file sharpness caching in `VCurveMethod` (currently re-measures
   all frames each round — fine for ≤20-frame scans, wasteful beyond).
