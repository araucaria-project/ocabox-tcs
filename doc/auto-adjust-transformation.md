# Auto-adjusting parametric transformations

**Status:** design pattern reference, broader than any single TCS service.
Applicable to several OCM subsystems (guider pulse-guide model, pointing
model, focuser, exposure calculator, OB time calculator). This document is
written as a standalone design piece — guider-specific concretisation lives
in the final section but the rest is general.

## 1. Problem framing

A subsystem needs a parametric transformation `f : X → Y` to convert some
input `x` (request + conditions) into an action or prediction `y`. Examples:

| Subsystem | Input `x` | Output `y` |
|---|---|---|
| Guider pulse-guide | (dx_px, dy_px, Alt, Az, derotator-angle) | (dtA_ms, dtB_ms) |
| Pointing model | (HA, Dec) commanded | (ΔHA, ΔDec) correction |
| Focuser | (temp, humidity, Alt, Az, filter, derotator) | focus_position |
| Exposure calculator | (target_mag, filter, seeing, sky_brightness, airmass) | exp_time |
| OB time calculator (CTC) | (filter sequence, exp times, slew distance, readout speed) | wallclock_time |
| Collimation (future) | (Alt, Az, temp) | (tip, tilt, focus) |

In each case:

1. We don't have a perfect closed-form `f` (mechanical imperfections, thermal
   drift, mount-specific quirks, atmospheric variance).
2. We can **observe** the actual outcome whenever we apply our predicted
   action, so we accumulate empirical points
   `(x_i, y_predicted_i, y_actual_i, σ_i, t_i)`.
3. Conditions in `x` change continuously through normal operation —
   covariates are sampled "for free" as the system runs.
4. We want the model to **converge automatically** without operator
   bookkeeping, while staying stable.

This is **online supervised learning** with closed-loop sampling and several
practical wrinkles below.

### 1.1 Distinguishing features (vs textbook online learning)

- **Closed loop**: applying `f(x; θ_t)` influences which `x_{t+1}` we observe
  next. Bad `θ` produces bad samples → biased data → worse `θ`. Stability
  matters.
- **Cold start**: at `t = 0` we have zero data and a default `θ_0`. The
  system must do something useful (or at least non-destructive) before the
  first update.
- **Heterogeneous uncertainty**: each observation has its own `σ_i` from the
  measurement source (centroid quality, astrometric solve residual,
  photometry SNR).
- **Drift**: `f` itself changes slowly (mechanical wear, thermal cycle,
  mount adjustments). Old data must be deweighted or dropped.
- **Pose dependence**: parameters often depend smoothly on continuous
  conditions (Alt, Az, Rot). The model isn't `θ` but `θ(conditions)`.
- **Per-domain physics priors**: focus has strong temperature linearity;
  exposure has Pogson + sky-model; pulse-guide has mount-geometry jacobian.
  Ignoring these is wasteful.

## 2. Where this fits in the literature

Several overlapping fields apply. None has a monopoly:

- **Online regression / online learning** — Recursive Least Squares (RLS),
  Stochastic Gradient Descent (SGD). The most direct framing — see §12 [L99],
  [LS83].
- **System identification / adaptive control** — Model Reference Adaptive
  Control (MRAC), self-tuning regulators, recursive parameter estimation.
  Closer to our closed-loop reality, especially for guider — see [AW95].
- **Bayesian sequential inference / Gaussian Process regression** — gives
  uncertainty estimates natively, smooth interpolation in covariate space.
  Excellent for low-data regimes and pose-dependent models — see [RW06]
  (free online textbook) and recent online-GP work [SHA19], [GoGP17],
  [LGP08], [SGP25].
- **State-space / Kalman filter** — when `θ` is treated as a slowly-drifting
  state with known process noise; principled but heavy setup. Sensor
  drift compensation literature is rich here [MDPI25], [PMC19].
- **Reinforcement learning (model-based)** — overkill for our deterministic
  problems; mentioned only to dismiss.
- **Sensor calibration in metrology** — practical literature on
  "self-calibrating instruments"; less mathematical, more checklist-style.

For our class of problems (low-dimensional input, smooth physical models,
moderate data rates) the most useful intersection is:
**Bayesian or recursive linear regression with smooth covariate dependence**,
optionally non-parametric (GP) when conditions warrant.

## 3. Common abstraction

```
Predict:   y_pred = f(x; θ_t)
Observe:   y_actual, σ                            (real measurement of outcome)
Record:    D ← D ∪ {(x, y_actual, σ, t)}           (with optional age weighting)
Update:    θ_{t+1} = U(θ_t, D)                    (chosen update rule)
```

Module operations:

- `predict(x) → (y_pred, uncertainty)` — primary use; uncertainty exposes
  whether to trust the prediction at this `x`.
- `record(x, y_actual, σ, t)` — log empirical point; may immediately update
  or just buffer.
- `update()` — explicit refit on accumulated data (some methods do this on
  every `record`, others batch).
- `reset()` — back to `θ_0` (recalibration, after meridian flip, etc.).
- `serialise() / load()` — persist across restarts, with metadata
  (n_samples, last_update, environment fingerprint).
- `is_calibrated() → bool` — false until enough trustworthy data accumulated;
  callers may degrade to safe defaults.
- `health_metrics() → dict` — recent prediction RMS error, sample density in
  current pose neighbourhood, time since last update.

Event hooks (optional):

- `on_calibration_complete()` — first time confidence threshold reached
- `on_drift_detected(metric)` — recent residuals exceed expected;
  caller may want to recalibrate or reduce action gain
- `on_uncertainty_high(x)` — predicting in a sparse covariate region

## 4. Method catalogue

Each method is a choice of update rule `U` and a corresponding model class for
`f`.

### 4.1 Active calibration ("probe and fit")

Deliberately apply known inputs, measure outputs, fit `f` from the resulting
mini-experiment. The classical PHD2 / Ekos guider calibration: send +RA,
−RA, +Dec, −Dec pulse pairs, observe pixel shifts, solve for `J` analytically.

- **Pros**: fast, accurate locally, simple, no online update needed
- **Cons**: requires uninterrupted dedicated time; only refreshes on demand;
  doesn't track drift between calibrations

**Best as bootstrap**, paired with another method for online maintenance.

### 4.2 Recursive Least Squares (RLS) with forgetting factor

Linear-in-parameters model `y = Φ(x) θ`. Sufficient statistics
`P_t = (Φᵀ Φ + λ I)⁻¹` updated incrementally per sample. Foundational
treatment in [LS83]; with forgetting factor variants surveyed in [Vah05].
Multiple-forgetting schemes for heterogeneous parameter time-scales: [PV15].

```python
# per observation (φ_i, y_i, σ_i):
K = P_old φ_i / (λ σ_i² + φ_iᵀ P_old φ_i)
θ_new = θ_old + K (y_i - φ_iᵀ θ_old)
P_new = (P_old - K φ_iᵀ P_old) / λ        # forgetting factor 0 < λ ≤ 1
```

- **Pros**: closed form, O(d²) memory and per-update compute, natural
  forgetting, weighted by σ
- **Cons**: linear in θ only — for nonlinear-in-θ functions, need feature
  engineering (`Φ(x)`) or change method
- **Forgetting factor λ**: controls memory horizon. λ = 0.99 → effective
  window ≈ 100 points. λ = 1 → full history.
- **Code size**: ~30 LOC

**Sweet spot**: linear models with moderate dimensions (d ≤ 20-50). Default
choice for many of our problems.

### 4.3 Gaussian Process (GP) regression

Non-parametric Bayesian: `y(x) ~ GP(μ(x), k(x, x'))`. Predictions and
uncertainties are closed-form in terms of the kernel and accumulated data.
Standard reference: [RW06] (free online textbook). Online / streaming GP
variants for our use case: sliding-window [LGP08], sequential GP for
nonstationary functions [SHA19], fast online regression [GoGP17],
mixture-of-experts streaming [SGP25].

- **Pros**: native uncertainty estimates, smooth interpolation in covariate
  space, works well with little data, kernel choice encodes prior smoothness
- **Cons**: O(N³) refit (or O(N²) sparse approximations); kernel
  hyperparameter selection needed; doesn't extrapolate well outside data
- **Memory**: full data buffer (sliding window of N ≈ 100-500 typical)
- **Libraries**: `scikit-learn.gaussian_process`, `GPy`, `gpytorch`
- **Code size**: ~50 LOC with sklearn

**Sweet spot**: smooth nonlinear `f(x)` with low-dim covariates (d ≤ 5-8) and
modest sample budget. Pose-dependent guider model with (Alt, Az, Rot) is a
natural fit.

### 4.4 Local linear / kNN regression

For each query `x`, fit a small linear model on the k nearest neighbours from
the buffer.

- **Pros**: simple, no global model, handles local nonlinearity, natural
  spatial weighting
- **Cons**: O(N) query time naively (kd-tree → O(log N)); needs enough
  density everywhere it queries; uncertainty is heuristic
- **Memory**: full buffer
- **Code size**: ~50 LOC

**Sweet spot**: when global parametric form is unclear and queries cluster
(stay near recently-collected points).

### 4.5 Polynomial regression with periodic refit

Choose a polynomial form `f(x; θ)` (e.g., 2nd order in (Alt, Az)), refit `θ`
on the full buffer every K samples or T seconds.

- **Pros**: simple to specify, gives interpretable `θ`, weighted refit by σ
  is straightforward
- **Cons**: refit cost O(N d²); polynomial extrapolates badly outside data;
  feature engineering required
- **Memory**: full buffer
- **Code size**: ~30 LOC (numpy polyfit + design matrix)

**Sweet spot**: physics-motivated low-order polynomials (focus vs temperature,
exposure SNR vs sky_brightness).

### 4.6 Kalman filter (state-space form of θ drifting)

`θ_t = θ_{t-1} + w_t` (process noise), `y_t = H_t θ_t + v_t` (measurement
noise). Standard Kalman update. Used heavily in sensor drift compensation
literature [MDPI25], [PMC19] — those papers also cover Extended Kalman
Filter for nonlinear cases.

- **Pros**: principled drift handling, native uncertainty propagation
- **Cons**: complex setup, needs process-noise model, easy to get wrong
- **Code size**: ~80 LOC

**Sweet spot**: well-instrumented systems with good prior models of drift
dynamics. Rare in our domain — listed for completeness.

### 4.7 Neural network

Multilayer perceptron / similar. `f(x; θ)` = NN with parameters θ updated by
SGD or full-batch.

- **Pros**: universal approximator
- **Cons**: data-hungry (10³–10⁶ samples), training brittle, poor
  uncertainty, loses interpretability
- **Code size**: depends, but always ≫ alternatives

**Almost always overkill** for our problems. Reserve for cases where strong
nonlinearities are confirmed and data is plentiful (Mirek's CTC may be one).

### 4.8 Comparison table

| method | update cost | memory | uncertainty | nonlinear in θ | drift |
|---|---|---|---|---|---|
| Active cal | one-shot O(d²) | none | residual fit | yes (via `Φ(x)`) | manual recal |
| RLS + λ | O(d²) per sample | O(d²) | from P matrix | linear-in-θ only | natural λ |
| GP | O(N³) refit | O(N) | native | yes (via kernel) | sliding window |
| Local linear | O(k) query | O(N) | heuristic | yes (locally) | buffer policy |
| Polynomial refit | O(N d²) refit | O(N) | from residuals | yes (via Φ) | buffer policy |
| Kalman | O(d²) per sample | O(d²) | native | linear-in-θ only | process noise |
| Neural net | many epochs | O(d²+) | poor | yes | hard |

For most OCM problems, the realistic candidates are **RLS, GP, and Polynomial
refit**. Active calibration is the standard bootstrap.

## 5. Stability and safety

The closed loop magnifies any model error. The following safeguards are
generally needed regardless of method choice.

### 5.1 Damping factor (action gain)

Apply only `α · y_pred` instead of `y_pred`, with `α ∈ (0, 1]` (typical
0.5–0.7 for early bootstrap, 0.9–1.0 once converged). Prevents over-correction
oscillation. Can be ramped up automatically as `is_calibrated()` becomes
true.

### 5.2 Deadband

If `|x|` (the requested correction in input space) is below a threshold,
return zero instead of applying. Avoids twitchy behaviour from noise.

### 5.3 Saturation / clipping

Hard limits on output magnitude. If `f(x; θ)` predicts something outside
plausible range (e.g., pulse longer than max safe duration), clip and emit
warning. Prevents runaway from numerical or modelling pathology.

### 5.4 Sanity / outlier rejection

Maintain running statistics of prediction-vs-outcome residuals. If a single
update would shift `θ` by more than N standard deviations of recent updates,
reject the sample as outlier (cosmic ray, mount glitch, etc.).

### 5.5 Asymmetric convergence guards

Some axes/parameters converge faster than others. If one axis's update would
exceed its current confidence interval by a large factor, freeze that axis's
update and emit a "needs recal" event.

### 5.6 Persistent storage with staleness fingerprint

`θ` should survive process restarts. Persisted state includes:
- `θ`, sufficient statistics (P matrix or buffer)
- `n_samples`, `last_update_ts`
- **Environmental fingerprint**: pose centroid, temperature range, equipment
  config hash. On load, compare fingerprint to current environment; if delta
  too large, mark `is_calibrated()` false and force recalibration.

### 5.7 Atomic update on calibration

Active calibration runs that fail (lost star, etc.) must not corrupt `θ`.
Use atomic swap: build candidate `θ_new` to completion, then atomically
replace.

### 5.8 Mode switches

Some operations (mount slew, filter change, focus shift, meridian flip)
invalidate parts or all of the model. Hooks:
- `notify_event(name, ...)` → method decides (reset all? freeze updates for
  N seconds? recalibrate?)

## 6. Module interface (proposal)

A clean Python interface that all our applications can share:

```python
from typing import Generic, Protocol, TypeVar

X = TypeVar("X")
Y = TypeVar("Y")


class AdaptiveTransform(Protocol, Generic[X, Y]):
    """Online-learning parametric transform x → y."""

    def predict(self, x: X) -> tuple[Y, float]:
        """Return (prediction, uncertainty). Uncertainty in output units."""

    def record(self, x: X, y_actual: Y, sigma: float, ts: float) -> None:
        """Add empirical point. May trigger immediate update."""

    def is_calibrated(self) -> bool:
        """Predictions trustworthy?"""

    def reset(self) -> None: ...

    def serialise(self) -> bytes: ...
    @classmethod
    def load(cls, data: bytes) -> "AdaptiveTransform[X, Y]": ...

    def health_metrics(self) -> dict: ...

    def notify_event(self, event: str, **payload) -> None:
        """Inform method of external context (slew, focus change, …)."""
```

Concrete implementations as classes implementing the protocol:

- `RLSAdapter[X, Y](feature_fn, forgetting=0.99)`
- `GPAdapter[X, Y](kernel, lengthscales, noise)`
- `LocalLinearAdapter[X, Y](k_neighbours, window_size)`
- `PolynomialAdapter[X, Y](order, refit_every)`
- `ActiveCalAdapter[X, Y](probe_sequence, fit_fn)` — bootstrap-only

Composable: a guider pulse-guide model could use `ActiveCalAdapter` for
bootstrap, then hand off `θ_0` to `RLSAdapter` for online refinement, with a
high-level `AdapterChain` choosing which to query based on
`is_calibrated()`.

## 7. Application: guider pulse-guide model

Specific instance: `f : (dx_px, dy_px, Alt, Az, Rot) → (dtA_ms, dtB_ms)`.

### 7.1 Physical structure

Most fundamentally, a 2×2 jacobian `J` mapping pixel error to mount-axis
time:

```
[dtA]   [J_AA J_AB] [dx]
[dtB] = [J_BA J_BB] [dy]
```

For an **equatorial** mount with no field rotation: `J` is roughly constant.
Four parameters suffice.

For an **alt-az** mount: `J` rotates with the parallactic angle, which is a
deterministic function of (Alt, Az, latitude, declination) [TM-AltAz].
Either:
- Explicit physical decomposition: `J(Alt, Az, Rot) = R(η) J_local R(Rot)⁻¹`
  where `η` = parallactic angle. Saves data, requires good geometry knowledge.
- Black-box: parameterise `J(Alt, Az, Rot)` directly via kernel/polynomial.
  More data needed.

The guider works on the residual error after this fundamental geometry, which
is dominated by mount mechanics (gear backlash, periodic error, encoder bias)
— a smaller, less structured term that fitting can capture.

### 7.2 Recommended approach (hybrid)

**Stage A — bootstrap via active calibration**:
- On `acquired = False → True` (first lock), or on operator command:
- Pause guiding. Push known pulses (+A, −A, +B, −B), measure pixel shifts.
- Solve `J_local` analytically. Optionally apply parallactic-angle rotation.
- Store as `θ_0` with pose fingerprint = current (Alt, Az, Rot).

This is the classical PHD2 / Ekos approach [PHD2] [Ekos]. Good practice is
to do the active cal near the celestial equator and meridian for clean
RA/Dec separation.

**Stage B — online refinement via RLS** (parameters of `J` linear in θ):
- After each pulse-guide event, record `(x = (dx, dy, conditions), y_actual
  = observed (Δdt_A, Δdt_B), σ, t)`.
- Update `J` (or its parameterised version) with weighted RLS.
- Forgetting factor λ ≈ 0.99 (effective horizon ~100 events).

**Stage C — pose-dependent J** (when alt-az or large-area observing
demands it):
- Replace global `J` with `J(Alt, Az, Rot)` modelled as either:
  - Polynomial: `J_ij(Alt, Az, Rot) = sum_k c_{ijk} φ_k(Alt, Az, Rot)`, RLS
    on `c`. Choose `φ_k` from prior physics (parallactic-angle rotation
    plus a few residual terms).
  - GP per element of `J` (4 GPs over 3D pose), with sliding window of
    recent observations.
- Choice between the two depends on data density and pose coverage; start
  with polynomial, evaluate.

**Recalibration triggers**:
- Meridian flip → reset (geometry sign flips)
- Filter / focuser change → freeze updates for N frames
- Recent residuals exceed expected RMS by >3× → request operator recal
- Pose movement > threshold from current data centroid → emit
  `on_uncertainty_high`, reduce damping

### 7.3 Stability for guider specifically

- Damping factor `α` ramps from 0.5 (during bootstrap) to 0.95 (after 50
  successful corrections within 3σ residual)
- Deadband: pixel error < 0.3 px → no pulse
- Pulse saturation: per-axis `pulse_max_ms` from config; clip and warn
- Outlier rejection: a single residual > 5σ rolling RMS rejects the sample
- Atomic active-cal: failure in stage A leaves previous `θ` intact

### 7.4 Persistent storage

`θ`, sufficient statistics, environment fingerprint persisted to ocadb (or
config-fs) every N updates and on graceful shutdown. Fingerprint includes
mount geometry parameters from `tic.config.observatory`; if those change, `θ`
is reset.

## 8. Other OCM applications (sketches)

Each application would benefit from the same abstraction:

### 8.1 Pointing model
- `f : (HA, Dec) → (ΔHA, ΔDec)` (or in (Alt, Az) for alt-az mounts)
- Canonical parametric form is **TPOINT** (Patrick T. Wallace, [Wal-TPOINT]):
  six geometrical zero-point terms (IH, ID, CH, NP, MA, ME) plus a few
  flexure / harmonic terms — all linear in θ. Surveyed in [TPOINT-94] and
  applied in many production telescopes (e.g., Green Bank Telescope
  pointing analysis [GBT22]).
- **RLS** with feature vector `Φ` = TPOINT term basis is the canonical fit.
  An astrometric solve of each science image gives one (commanded, actual)
  data point — continuous refinement is "free" with no probe overhead.
- Modern alternative: quaternion-based formulation [Rie18] for rapid recal.

### 8.2 Focuser
- `f : (temp, humidity, Alt, Az, filter, derotator) → focus_position`
- Strong physics: thermal expansion is dominant linear term in `temp`
- Polynomial of temp + per-filter offset + small (Alt, Az) correction
- **RLS or polynomial refit**, possibly **GP** if (Alt, Az) correction is
  significant
- Bootstrap from "v-curve" focus runs; refine from every successful
  in-sequence focus

### 8.3 Exposure time calculator
- `f : (target_mag, filter, seeing, sky_brightness, airmass) → exp_time`
- Physics gives 70% (Pogson + sky-noise + atmosphere); residual ~20-30% from
  per-detector / per-night idiosyncrasies
- Hybrid: physics base function + ML on residual
- **RLS on residual** with engineered features (multiplicative corrections
  per filter, additive per camera) is sufficient for our scale

### 8.4 OB time calculator (CTC)
- `f : (sequence specification) → wallclock_time`
- Mostly summing known components (exp_time × repetitions + readout × n +
  slew × distance + settle + filter changes)
- Residual dominated by slow disk writes, network jitter, occasional retries
- Mirek's ML approach is reasonable for residual modelling. The structured
  base function handles 90%; **RLS/GP on residual** handles the rest.

### 8.5 Common observation
All four of these share:
- **Strong physics priors** for the bulk of `f`
- **Residual modelling** is what's actually being learned
- Same persistence, drift, and stability concerns as guider

If we factor `f = f_physics(x) + g(x; θ)` and learn `g`, the abstract module
operates on `g` — `f_physics` is just code provided by the application.

## 9. Implementation path for guider in TCS

Staged so each step is shippable:

**Stage 0 — bootstrap-only static `J`** (frame iteration of guider):
- Operator-triggered active calibration via `calibrate_pulse_guide()` RPC
- Compute `J_local` analytically from 4 probe pulses
- Store in `tic.config.observatory` (per-mount) or per-pipeline config file
- Use `J_local` statically; no online update
- Recalibrate manually after meridian flip etc.

**Stage 1 — online RLS refinement** (post-frame):
- Add `record(x, y_actual, σ, t)` from each pulse-guide event
- Implement `RLSAdapter` with forgetting `λ = 0.99`
- Stability guards (damping, deadband, saturation, outlier rejection)
- Persist `(θ, P)` periodically + on shutdown

**Stage 2 — pose-aware `J(Alt, Az, Rot)`**:
- Add pose features to `Φ(x)`
- Start with parallactic-angle rotation (physics) + polynomial residual
- Evaluate vs Stage 1 on real telescope data; only ship if measurable benefit

**Stage 3 — extract `AdaptiveTransform` abstraction** (conditional):
- Trigger: when a second OCM service (pointing model? focuser?) needs the
  same pattern
- Move `RLSAdapter` and friends into a shared module (initial home:
  `ocabox_tcs/adaptive/`; later candidate: separate package
  `oca-adaptive` or extension to pyaraucaria depending on adoption)
- Refactor guider to use the abstract module via dependency injection

**Stage 4 — instantiate other domains** (one-by-one as they need it).

## 10. Open questions and what to test

- **λ value for guider RLS**: 0.99 is a guess. Should be tuned by observing
  prediction RMS vs true error over an observing run. Span 0.95–0.999.
- **Buffer size for GP/local methods**: 200? 500? Depends on memory, compute
  budget, data rate. Test in production-like setting.
- **Pose-feature engineering**: do we use parallactic-angle decomposition or
  black-box? Decision needs real alt-az data to make.
- **Persistence venue**: ocadb vs config-file-fs vs `tic.config.observatory`
  publish-back. Three legitimate options; depends on read/write semantics
  and integrity guarantees the chosen store provides.
- **Cross-restart equivalence**: how strict should the environment fingerprint
  match be? A 1°C temperature change probably shouldn't invalidate `θ`; a
  filter wheel re-mount should.
- **Calibration data sharing across services**: pointing-model active-cal
  collects (commanded, actual) pairs that are *also* useful to a guider's
  pose-aware `J`. Worth exposing via NATS event bus so multiple consumers
  can build their own models from shared raw data.

## 11. General opinion

Yes, this is a real recurring pattern in OCM, and yes, an abstract module is
worth building **eventually** — but **not from day one**. The risk of
abstracting before the second concrete instance is that we shape the
interface around guider's quirks and discover later it doesn't quite fit
pointing or focuser, requiring breaking changes.

Recommended sequence:
1. **Now**: write this design doc (done). Implement guider's pulse-guide
   model concretely in `ocabox_tcs/services/guiding_svc/pulse_guide.py`,
   following the patterns described here (predict / record / persist /
   stability guards) but without forcing the abstract `Protocol`.
2. **When pointing model or focuser are ready for refresh**: revisit. If
   patterns truly recur, refactor toward the abstraction. The interface in
   §6 is a starting point, not a contract — expect to adjust on second
   implementation.
3. **If patterns diverge**: that's also fine — keep them as separate concrete
   modules, don't force commonality.

The doc is most valuable as a **vocabulary and decision-record** at design
time of *each* application — not necessarily as the foundation of a shared
library.

## 12. References

Curated starting set. Citations in earlier sections use the bracketed
shorthand below.

### Foundational textbooks

- **[L99]** Ljung, L. (1999). *System Identification: Theory for the User*
  (2nd ed.). Prentice Hall, ISBN 0-13-656695-2. Comprehensive treatment of
  identification methods; includes a dedicated chapter on recursive
  estimation. Standard reference for the field.
- **[LS83]** Ljung, L., & Söderström, T. (1983). *Theory and Practice of
  Recursive Identification*. MIT Press. The dedicated treatise on online
  identification — RLS variants, convergence, drift handling. Older but
  foundational.
- **[AW95]** Åström, K. J., & Wittenmark, B. (1995). *Adaptive Control*
  (2nd ed.). Addison-Wesley. Reprinted by Dover (2008). Classical reference
  for self-tuning regulators, model-reference adaptive control, real-time
  parameter estimation. Pairs the theory with implementation guidance.
- **[RW06]** Rasmussen, C. E., & Williams, C. K. I. (2006). *Gaussian
  Processes for Machine Learning*. MIT Press. **Free online**:
  https://gaussianprocess.org/gpml/. The standard GP textbook; chapters 2–3
  cover the regression case directly relevant to us.

### Recursive least squares — recent variants

- **[Vah05]** Vahidi, A., Stefanopoulou, A., & Peng, H. (2005). Recursive
  Least Squares with Forgetting for Online Estimation of Vehicle Mass and
  Road Grade. *Vehicle System Dynamics*. PDF:
  https://cecas.clemson.edu/~avahidi/wp-content/uploads/2016/11/vsd2004vahidi.pdf.
  Practical RLS variant with multiple forgetting schemes; close in spirit to
  our setting.
- **[PV15]** Paleologu, C., Benesty, J., & Ciochină, S. (2015). A New
  Recursive Least-Squares Method with Multiple Forgetting Schemes.
  arXiv:1503.07338. https://arxiv.org/pdf/1503.07338. For systems where
  different parameters drift at different rates.

### Gaussian Process — online / streaming

- **[LGP08]** Nguyen-Tuong, D., Seeger, M., & Peters, J. (2008). Local
  Gaussian Process Regression for Real Time Online Model Learning. *NeurIPS
  2008*. https://proceedings.neurips.cc/paper/2008/file/01161aaa0b6d1345dd8fe4e481144d84-Paper.pdf.
  Locality-weighted GP for real-time control; small fixed-size local models.
- **[GoGP17]** Le, T., Nguyen, K., & Phung, D. (2017). GoGP: Fast Online
  Regression with Gaussian Processes. *IEEE ICDM 2017*.
  https://ieeexplore.ieee.org/document/8215498/. Fast online updates without
  full retraining.
- **[SHA19]** Zhang, M., Bird, T., Habib, R., et al. (2019). Sequential
  Gaussian Processes for Online Learning of Nonstationary Functions.
  arXiv:1905.10003. https://arxiv.org/pdf/1905.10003. Mixture of GPs for
  drifting / nonstationary `f`.
- **[SGP25]** Streaming Generated Gaussian Process Experts for Online
  Learning and Control (extended version, 2025). arXiv:2508.03679.
  https://arxiv.org/html/2508.03679. Recent work on partitioning streams
  across multiple GP experts for control applications.

### Sensor calibration & Kalman drift

- **[MDPI25]** Adaptive Kalman Filtering for Compensating External Effects
  in On-Line Spectroscopic Measurements. *MDPI Sensors* 25(8):2513 (2025).
  https://www.mdpi.com/1424-8220/25/8/2513. Practical adaptive-Kalman recipe
  for an analytical instrument — close pattern to our subsystems.
- **[PMC19]** The Use of Kalman Filtering and Correlation Techniques in
  Analytical Calibration Procedures. *PMC*.
  https://pmc.ncbi.nlm.nih.gov/articles/PMC6644963/. Survey of Kalman-based
  calibration in analytical chemistry; tutorial.

### Telescope-specific

- **[Wal-TPOINT]** Wallace, P. T. *Telescope Pointing*. Foundational paper /
  manual on the TPOINT model (six geometrical terms + flexure + harmonic).
  Bisque hosts a public version:
  https://www.bisque.com/wp-content/uploads/2022/10/Telescope-Pointing.pdf
- **[TPOINT-94]** Wallace, P. T. (1994). *TPOINT — Telescope Pointing
  Analysis System (v4.4)*. Starlink user note.
  https://sites.astro.caltech.edu/~srk/TP/Literature/Tpoint_SunWorks.pdf
- **[Tpoint-Wiki]** "Tpoint" — Wikipedia overview.
  https://en.wikipedia.org/wiki/Tpoint
- **[GBT22]** Frayer, D. T., et al. (2022). Green Bank Telescope: Overview
  and analysis of metrology systems and pointing performance. *Astronomy &
  Astrophysics*. https://www.aanda.org/articles/aa/full_html/2022/03/aa41936-21/aa41936-21.html.
  Production telescope pointing analysis — concrete real-world example.
- **[Rie18]** Riesing, K. M., et al. (2018). Rapid telescope pointing
  calibration: a quaternion-based solution using low-cost hardware. *J.
  Astronomical Telescopes, Instruments, and Systems* 4(3):034002.
  https://www.spiedigitallibrary.org/journals/journal-of-astronomical-telescopes-instruments-and-systems/volume-4/issue-03/034002/.
  Modern rapid-cal alternative to full TPOINT runs.
- **[PHD2]** Open PHD2 Guiding documentation and source.
  Docs: https://openphdguiding.org/man/. Source:
  https://github.com/OpenPHDGuiding/phd2. Reference implementation of
  guider calibration.
- **[Ekos]** KStars/Ekos guider documentation and source.
  Docs: https://kstars-docs.kde.org/en/user_manual/ekos-guide.html.
  Source under `kstars/ekos/guide/` in the KStars repo
  (https://github.com/KDE/kstars). Aligned-philosophy open-source project,
  default detection algorithm is **SEP MultiStar** (using up to 50
  reference stars via the SEP library); also implements **GPG RA Guiding**
  per [KZSH16].
- **[KZSH16]** Klenske, E. D., Zeilinger, M. N., Schölkopf, B., & Hennig, P.
  (2016). Gaussian Process-Based Predictive Control for Periodic Error
  Correction. *IEEE Transactions on Control Systems Technology* 24(1),
  110–121. PDF: https://ei.is.tuebingen.mpg.de/uploads_file/attachment/attachment/9/Klenske_tcst__1_.pdf.
  **The paper behind PHD2 PPEC and Ekos GPG.** ML researchers at MPI
  Tübingen apply locally-periodic GP regression to telescope auto-guiding
  periodic-error correction. Direct cross-domain validation that GP
  methods fit our setting.
- **[TM-AltAz]** Telescope Mounts Explained. Astrophotography with Alt-Az
  telescope mounts.
  https://telescopemount.org/astrophotography-with-alt-az-telescope-mounts/.
  Practical issues with field rotation and rotator-vs-no-rotator workflows.

### Cross-domain: robotics calibration & online ID

- **[RT99]** Roy, N., & Thrun, S. (1999). Online Self-Calibration For
  Mobile Robots. *Proc. ICRA 1999*. Free PDF:
  https://www.cs.cmu.edu/~thrun/papers/roy_icra_calib.pdf. **The classic**
  online self-calibration paper. Statistical method for calibrating mobile
  robot odometry incrementally during normal operation; demonstrated
  order-of-magnitude error reduction in real museum-tour deployments.
  Direct precursor to our pattern.
- **[HC16]** He, R., Zhao, Y., Yang, S., & Yang, S. (2016). POE-Based
  Robot Kinematic Calibration Using Axis Configuration Space and the
  Adjoint Error Model. *IEEE Transactions on Robotics* 32(5), 1077–1093.
  https://ieeexplore.ieee.org/iel7/8860/4359257/07551163.pdf. The flagship
  modern formulation: POE on the joint-axis configuration manifold with
  multiplicative adjoint error. Eliminates parameter redundancy of
  Denavit-Hartenberg-style models. Mathematical bridge to telescope
  pointing models — see §14.
- **[POE-Robotica]** He, R. et al. (later refinements):
  - *An improved minimal error model for robotic kinematic calibration
    based on the POE formula* (Robotica).
    https://www.cambridge.org/core/journals/robotica/article/abs/an-improved-minimal-error-model-for-the-robotic-kinematic-calibration-based-on-the-poe-formula/AABDEC45364C9A337AB9143475052E9F
  - *POE-based kinematic calibration for serial robots using
    left-invariant error representation and decomposed iterative method*
    (J. Mech. & Mach.).
    https://www.sciencedirect.com/science/article/abs/pii/S092188902400280X
  - *Position-Based Robot Calibration and Compensation Using an Improved
    Adjoint Error Model* (J. Intel. Robotic Sys. 2023).
    https://link.springer.com/article/10.1007/s10846-023-01891-6

### Cross-domain: model-free adaptive control

- **[HJ13]** Hou, Z., & Jin, S. (2013). *Model Free Adaptive Control:
  Theory and Applications*. CRC Press / Routledge, ISBN 978-1138033962.
  Comprehensive treatment of MFAC families: compact-form, partial-form,
  full-form dynamic linearisation; pseudo Jacobian / pseudo gradient
  estimators; convergence analysis under generalised Lipschitz
  conditions.
  https://www.routledge.com/Model-Free-Adaptive-Control-Theory-and-Applications/Hou-Jin/p/book/9781138033962
- **[MFAC-PMC23]** An improved compact-form antisaturation model-free
  adaptive control algorithm for a class of nonlinear systems with time
  delays. *PMC*, 2023.
  https://pmc.ncbi.nlm.nih.gov/articles/PMC10631356/. Recent application
  paper showing MFAC in practice.

### Useful surveys / blog-level pieces

- *Recursive Least Squares with Forgetting Factor* — concise overview,
  Emergent Mind. https://www.emergentmind.com/topics/recursive-least-squares-rls-with-forgetting-factor
- *Adaptive FRIT-based Recursive Robust Controller Design Using Forgetting
  Factors* — recent applied paper. arXiv:2402.00384.
  https://arxiv.org/html/2402.00384v1

### How to use these references

For our **immediate guider work**: [LS83] §3 (RLS basics), [Vah05] (forgetting
factor practice), [Ekos] + [PHD2] (concrete guider-calibration procedure
and the SEP-MultiStar / GPG algorithms), [KZSH16] (the GP-PEC paper if we
ever do periodic-error compensation).

For **future pointing-model work**: [Wal-TPOINT] (term taxonomy), [TPOINT-94]
(official manual), [GBT22] (real production-telescope analysis).

For **future pose-aware extensions**: [RW06] §2 (GP regression), [LGP08] +
[SHA19] (online streaming variants).

For **future Kalman-style drift modelling** (if we go that direction):
[MDPI25], [PMC19] (applied recipes), [AW95] §3 (theoretical foundation).

For **cross-domain transfer / paper potential** (§14): [RT99] (classic
online self-cal), [HC16] (POE adjoint error model — the framework to
adapt), [KZSH16] (precedent of ML/control researchers tackling telescope
problems and publishing). Worth a careful lit search for "telescope
pointing model online learning" / "pointing model bundle adjustment"
before claiming novelty — the math is general enough that someone may
have done it.

For **MFAC interest** (independent of guider): [HJ13] textbook is the
single comprehensive reference; [MFAC-toolbox] and [MFAC-PMC23] for
applied flavour.

## 13. Available implementations and reference projects

Before rolling anything ourselves, useful to know what's out there. Tiered by
how directly we can use it.

### Tier 1 — use directly (off-the-shelf libraries)

These are small, focused, well-maintained, and slot in cleanly.

- **[padasip]** Python Adaptive Signal Processing — `pip install padasip`.
  Pure-NumPy. `padasip.filters.FilterRLS(n)` gives RLS with forgetting factor
  out of the box. ~30 lines to integrate as Stage 1 (online refinement) of
  guider pulse-guide model. **Recommended for our RLS needs.**
  Source: https://github.com/matousc89/padasip / docs:
  https://matousc89.github.io/padasip/sources/filters/rls.html. Paper:
  Cejnek & Vrba (2022) Padasip: An open-source Python toolbox for adaptive
  filtering.
- **[sklearn-GP]** `sklearn.gaussian_process.GaussianProcessRegressor`. Built
  into scikit-learn (already a transitive dep via numpy/scipy). Batch GP fit
  with kernel selection. **Use for initial GP exploration.** Refit on a
  sliding window for online behaviour (cost: O(N³) refit; OK for
  N ≤ ~500). Docs:
  https://scikit-learn.org/stable/modules/gaussian_process.html
- **[filterpy]** Roger Labbe's Kalman filter library. `pip install filterpy`.
  Standard implementation if we go Kalman route. Docs:
  https://filterpy.readthedocs.io/

### Tier 2 — worth knowing, may grow into

Heavier or more specialised; consider when scaling concerns matter.

- **[river]** Online machine learning in Python — `pip install river`.
  Result of merging `creme` + `scikit-multiflow`. Streaming regression
  (LinearRegression, HoeffdingAdaptiveTreeRegressor, SGTRegressor),
  classification, anomaly detection, drift detection. Per-sample
  `learn_one()` / `predict_one()` API. **Fit for: residual modelling in
  CTC-like cases** (Mirek's OB time calculator) where you want a tree-based
  online learner with concept-drift handling. Probably overkill for our
  low-dim parametric models. Paper: arXiv:2012.04740. Source:
  https://github.com/online-ml/river. Docs: https://riverml.xyz/
- **[gpytorch]** GPU-accelerated, autodiff-friendly GP via PyTorch. Scales
  to N ≫ 1000 via approximate inference (variational, sparse). **Fit for:
  pose-aware J(Alt, Az, Rot) if data accumulates beyond sklearn's reach.**
  Adds PyTorch as a fat dep. Source: https://github.com/cornellius-gp/gpytorch.
  Docs: https://gpytorch.ai/

### Tier 3 — ecosystem-aware (not directly applicable)

Adjacent communities; useful to know they exist.

- **[python-control]** Python Control Systems Library. Classical
  control-systems toolbox: state-space, transfer functions, simulation,
  design tools (LQR, Bode, root-locus). **Not** an online-learning tool.
  Use if we ever do controller design (e.g., tuning a PID compensator on
  top of pulse-guide). Source:
  https://github.com/python-control/python-control
- **[do-mpc]** Model Predictive Control toolbox. For optimisation-based
  control, requires explicit dynamics model. Out of scope for our adaptive
  *transformation* problem; in scope if we ever do trajectory-following on
  the mount. https://www.do-mpc.com/
- **[SIPPY]** Systems Identification Package for Python. Batch system
  identification (transfer-function fits, state-space estimation) for MIMO
  linear systems. Useful for **offline analysis** of recorded data —
  characterising mount dynamics from a captured run, then designing a
  controller. Not online. Source:
  https://github.com/CPCLAB-UNIPI/SIPPY
- **[SysIdentPy]** Nonlinear system identification (NARMAX models). Same
  offline batch flavour. https://sysidentpy.org/
- **[MFAC]** Model-Free Adaptive Control toolbox. Different paradigm —
  doesn't try to learn the model, just adjusts control law via dynamic
  linearisation. Mentioned for completeness; doesn't fit our "build a
  parametric model" framing.
  https://github.com/shahind/MFAC

### Tier 4 — reference implementations of guiders (read for inspiration)

Concrete code we can study when implementing our guider, even if we don't
import any of it. Open-source / aligned-philosophy projects only.

- **[INDI-Ekos]** KStars/Ekos has a built-in internal guider plus support
  for external guiders (PHD2, lin_guider). Aligned-philosophy project
  worth following actively. https://www.indilib.org/. Source under
  `kstars/ekos/guide/` in the KStars repo
  (https://github.com/KDE/kstars). Two algorithm features worth studying:
  - **SEP MultiStar** — default detection & drift algorithm, uses up to
    50 stars (via the SEP library — Source Extractor in Python/C). Robust
    to single-star loss. Direct relevance to our `multi_star` Solver
    method.
  - **GPG RA Guiding** — Gaussian Process predictive correction for RA
    periodic error. Same algorithm as PHD2's PPEC, originally from
    Klenske et al. 2016 [KZSH16] at MPI Tübingen.
- **[PHD2]** OpenPHDGuiding — widely-deployed amateur/pro guider, C++.
  Full active-calibration + dec backlash compensation + PPEC. Source:
  https://github.com/OpenPHDGuiding/phd2. Read `Guider*.cpp` and the PEC
  algorithm files for the concrete algorithmic choices and edge-case
  handling.
- **[lin_guider]** Linux guider, GPL3. Smaller codebase than PHD2,
  potentially easier to skim for the core algorithm without amateur-mount
  dec-backlash machinery.
  Source: https://sourceforge.net/projects/linguider/ (and forks on GitHub).

### Tier 5 — cross-domain: robotics calibration (the most interesting one)

Not astronomy, but the *exact* same problem class with a more developed
mathematical apparatus. Robotics has gone deep on this — Lie groups,
manifold-aware estimators, online identifiability — well past where
TPOINT and PHD2 stopped. Worth substantial study; potentially worth a
direct technology transfer (and a paper). See §14 for the cross-domain
bridge in detail.

Software:

- **[robot_calibration]** ROS package for generic robot calibration. The
  optimisation framing — "minimise reprojection error of checkerboard
  corners across all observations to refine kinematic parameters" — is
  identical in spirit to our "minimise residual of astrometric solves to
  refine pointing model parameters". Calibration-as-bundle-adjustment.
  Source: https://github.com/mikeferguson/robot_calibration
- **[pybotics]** Python toolbox for robot kinematics and calibration.
  Cleaner standalone library (not ROS-coupled). Demonstrates kinematic
  parameter optimisation from end-effector observations. Source:
  https://pypi.org/project/pybotics/
- **[philnad-poe]** POE-based robot kinematic calibration using axis
  configuration space and the adjoint error model. Implements the method
  from [HC16] in pure Python on top of Robotics Toolbox. Mathematical
  interest: linearises kinematic error in tangent space (Lie algebra) —
  exact same idea as TPOINT's geometric terms but as a *general framework*
  rather than telescope-specific term taxonomy.
  Source: https://github.com/PhilNad/robot-arm-kinematic-calibration
- **[neuebot-kincal]** Automatic kinematic calibration via circle fitting
  and dual vector geometry. Different math approach to the same problem.
  Source: https://github.com/neuebot/Kinematic-Calibration

Foundational papers (more in §12):

- **[RT99]** Roy & Thrun, *Online Self-Calibration For Mobile Robots*,
  ICRA 1999. **The classic** online self-calibration paper, free PDF.
  Incremental maximum-likelihood algorithm for adapting odometry
  parameters during normal operation. Direct precursor to the pattern we
  want for the guider's pulse-guide model.
- **[HC16]** He, Chen et al., *POE-Based Robot Kinematic Calibration
  Using Axis Configuration Space and the Adjoint Error Model*, IEEE
  Transactions on Robotics 2016. The flagship modern formulation.
- **[KZSH16]** Klenske, Zeilinger, Schölkopf, Hennig, *Gaussian
  Process-Based Predictive Control for Periodic Error Correction*, IEEE
  TCST 2016. Cross-domain itself: ML researchers (Hennig is a leading
  Bayesian-numerics figure) tackled telescope auto-guiding as a control
  problem. **This is the paper behind PHD2 PPEC and Ekos GPG.**

### Tier 6 — independent interest: model-free adaptive control (MFAC)

Worth reading on its own merits even if we don't adopt it. Hou & Jin's
**Model-Free Adaptive Control** [HJ13] is a coherent alternative paradigm
that **doesn't try to identify the plant model at all** — instead it works
with a *time-varying linearised representation* derived purely from
input-output data, called the **pseudo Jacobian matrix** (PJM).

Key concepts:

- **Compact-Form Dynamic Linearisation (CFDL)**: locally, every smooth
  plant `y_{k+1} = f(y_k, u_k)` can be approximately written as
  `Δy_{k+1} = φ_k · Δu_k` for some time-varying scalar/matrix `φ_k`. `φ`
  is the *pseudo partial derivative / pseudo Jacobian*.
- **No model identification**: `φ_k` is updated online via projection
  algorithm (a parameter estimator) using only observed `(u_k, y_k)`.
- **Control law**: pick `Δu_k` to drive the next `Δy_{k+1}` toward the
  desired tracking error. Stability proven under Lipschitz-type
  assumptions on the (unknown) plant.
- **Variants**: Partial-Form (PFDL) and Full-Form (FFDL) Dynamic
  Linearisation extend to more memory of past inputs/outputs.
- **Where it shines**: nonlinear plants where building a parametric
  model is impractical or too costly (chemical processes, soft robotics,
  electrochemistry).

For our problem space MFAC is intriguing but probably **not the right tool
for guider's pulse-guide model** — that has clear physical structure (J as
2×2 matrix on smooth pose) and we benefit from physics priors. MFAC
shines when physics priors are weak. **Possible future fit**: focuser
behaviour with hysteresis, or any subsystem where dynamics are too
complex to model parametrically.

References:

- **[HJ13]** Hou, Z., & Jin, S. (2013). *Model Free Adaptive Control:
  Theory and Applications*. CRC Press / Routledge. The textbook —
  comprehensive treatment of MFAC families.
  https://www.routledge.com/Model-Free-Adaptive-Control-Theory-and-Applications/Hou-Jin/p/book/9781138033962
- **[MFAC-toolbox]** Python toolbox (CFDL, PFDL, FFDL for SISO + MIMO):
  https://github.com/shahind/MFAC. Worth a look to see the algorithms
  concretely.

### Recommendation summary for our path

**Stage 0 (active calibration bootstrap)**: ~50 LOC analytical solve, no
library needed. Mirror the [PHD2] / [Ekos] active-cal procedure (4
±X / ±Y guide pulses near the celestial equator and meridian for clean
RA/Dec axis separation).

**Stage 1 (RLS online refinement)**: **Use `padasip.FilterRLS`**. Saves us
~50 LOC and gets us tested forgetting-factor logic. Wrap with our adapter
interface (§6).

**Stage 2 (pose-aware J)**: **Try `sklearn` GP first** (smallest dep);
upgrade to `gpytorch` only if data scale demands. For polynomial fallback:
`numpy.polynomial` or hand-written feature design matrix.

**Stage 3 (extract abstract module)**: keep adapter pattern thin so
swapping implementations (padasip → river → custom) is mechanical.

**Don't pull**: river / control libs / robotics calibration libs as
production deps. They're for inspiration / inspection, not for our small
parametric models.

## 14. Cross-domain bridge: robotics calibration ↔ telescope pointing & guiding

Our adaptive transformations and robotics kinematic calibration are the
**same mathematical problem** with different domain object. Spelling out the
bridge unlocks (a) free use of robotics' more developed math toolkit, and
(b) genuine paper potential — interdisciplinary cross-pollination is rare and
publishable.

### 14.1 Mathematical correspondence

| Robotics (manipulator) | OCM (mount + guider) |
|---|---|
| Joint angles `q ∈ ℝⁿ` | Mount-axis encoder readouts `(HA, Dec)` or `(Alt, Az)` |
| Forward kinematics `T(q) ∈ SE(3)` | Pointing forward map: encoder → sky direction |
| End-effector pose error `ΔT` | Astrometric residual (commanded vs solved) |
| Joint twists `ξ_i ∈ se(3)` (Lie algebra of SE(3)) | TPOINT geometric error terms (zero-points, collimation, non-perpendicularity) |
| Adjoint error model on `ξ` | Linear-in-θ TPOINT model |
| POE formula `T = exp(ξ_1 q_1) · … · exp(ξ_n q_n)` | Mount geometry written multiplicatively as rotations on the celestial sphere |
| Online refinement from sensor observations | Online refinement from astrometric solves of imaging data |

The **adjoint error model** [HC16] linearises the kinematic error of a robot
in the *tangent space* (Lie algebra `se(3)`) of the configuration manifold
SE(3). Each parameter perturbation maps to an additive twist; errors compose
multiplicatively as Adjoint transformations of those twists. This:

- Avoids the parameter-redundancy and discontinuity issues of "naive"
  parameterisations (Denavit-Hartenberg)
- Is **continuous and complete** as an error parameterisation — a direct
  improvement over hand-crafted term taxonomies like TPOINT
- Maps cleanly to **multiplicative Kalman filter** updates if we go that
  way (compose perturbations, don't add them in extrinsic coordinates)

For an **alt-az mount with a derotator**, the mount has 3 axes
(Alt, Az, Rot) — a 3-DOF kinematic chain in SO(3) — directly addressable
by POE. Equatorial mounts are a 2-DOF chain. The TPOINT 6-term geometric
model is the *first-order* expansion of an SE(3) (or SO(3)) error map; the
adjoint error model gives the *fully geometric* version with all the same
information but no redundancy.

### 14.2 What we can borrow

Concrete techniques from robotics that map to our problem:

1. **Online adjoint-error estimator** [HC16] — incremental update of
   per-axis twist perturbations from observed pointing residuals. Replaces
   monolithic TPOINT batch fits with continuous online learning.
2. **Identifiability analysis** — robotics literature has rigorous methods
   for determining which parameters are observable from which motion
   trajectories. Maps to: which TPOINT terms are observable from
   astrometric solves at our typical sky coverage? Unconverged terms
   should be flagged.
3. **Bundle adjustment formulation** — calibration as a single
   nonlinear-least-squares problem over all observations and all
   parameters jointly. Standard in computer vision / SLAM /
   `robot_calibration`. Maps to: our pointing model fit becomes a
   bundle-adjustment over a window of recent astrometric solves.
4. **SLAM-style simultaneous estimation** — robotics simultaneously
   estimates kinematic parameters and environment / sensor parameters
   [RT99]. For us: simultaneously refine pulse-guide jacobian *and*
   pointing model from the same astrometric stream — they share data.
5. **Lie-algebra parameter persistence** — store θ in Lie algebra
   coordinates rather than Euclidean (matters when restoring from disk
   into a system that's already moved).

### 14.3 Paper potential

The story writes itself:

> **"Online robotic-calibration techniques applied to optical-telescope
> pointing and auto-guiding"**
>
> Hospital observatories rely on hand-crafted pointing models (TPOINT,
> 1990s) and active-calibration auto-guider routines (PHD2 / Ekos) that
> predate the modern systematic theory of kinematic calibration developed
> in robotics over the last two decades (POE / adjoint error model,
> manifold-aware estimators). We show that the telescope pointing-model fit
> is structurally a robot kinematic calibration problem; we apply
> POE-based online identification with adjoint-error estimation to a small
> telescope, and demonstrate (i) reduction in pointing residuals
> vs TPOINT terms-only fit, (ii) automatic identifiability flagging for
> unconverged parameters, (iii) joint estimation of pointing model and
> auto-guider pulse-guide jacobian from a single astrometric stream.

This is a real publishable contribution — the math is well-understood in
robotics and the application to telescopes appears to be unrepresented in
the literature (worth a careful lit search before claiming so). Suitable
venues: SPIE Astronomical Telescopes & Instrumentation; Journal of
Astronomical Telescopes, Instruments, and Systems; Experimental Astronomy.

### 14.4 Recommendation

Treat the cross-domain transfer as a **second-iteration goal**, not
day-one work. The order is:

1. **Now**: ship a basic guider with the patterns from §7 (active cal +
   RLS online refinement) — get something working in production.
2. **Soon after**: instrument so we record `(commanded, observed,
   residual, σ, t, pose)` tuples to a persistent log — the same log feeds
   pointing model, focuser, exposure calculator. Treat this as the
   "calibration data lake".
3. **Later**: revisit with the robotics framework — POE adjoint error,
   online refinement on the manifold, joint identification across
   subsystems. Publish the methodology paper.

The data captured in step 2 retains its value regardless of which
estimation framework we eventually adopt, so step 2 is the highest
expected-value work and should not block on the methodology choice.
