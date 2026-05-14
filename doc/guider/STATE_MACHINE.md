# Guider — kompletna logika stanu i akcji

Ten dokument jest źródłem prawdy dla zachowania guidera. Każdy patch
powinien zostać zweryfikowany przeciw temu doc'owi, nie odwrotnie.

## Pola stanu i ich życie

### Pozycje na sensorze

| Pole | Co to | Kiedy zmienia |
|---|---|---|
| `acquired_pos: (x,y) \| None` | Gdzie solver aktualnie ma lock na gwiazdę (sub-pixel). | Każda udana detekcja (`notify_acquired`); `lock_at` od operatora (na (x,y)). |
| `central_point: (x,y)` | Operatorski **target reticle** — gdzie chce wprowadzić gwiazdę. | YAML config init; `acquire_at` (right-click); "home" button. |
| `guide_anchor: (x,y) \| None` | Pozycja, **do której guider próbuje doprowadzić** gwiazdę. Solver liczy `correction = acquired_pos − guide_anchor`. | Patrz "Cykl guide_anchor" niżej. |
| `predicted_pos: (x,y) \| None` | Przewidywana pozycja gwiazdy po właśnie wydanym pulsie. | Pisane przez enforcer po pulsie; czyszczone przez controller po udanej re-akwizycji LUB przez `lock_at` (operator reset). |
| `last_acquired_pos: (x,y)` | Ostatnia *udana* detekcja. Używane przez wide-search smart-sort. | Każda udana detekcja. |

### Aktywny puls (Phase 1+2 data model)

`active_pulse: PulseEvent | None` — first-class temporal record:
```
issued_utc, motion_end_utc, settled_utc   — okna czasowe
src_pos, predicted_pos                     — endpointy trajektorii
pulse_t_n_ms, pulse_t_e_ms                 — wydane czasy (signed)
correction_dx_px, correction_dy_px         — błąd który puls ma skasować
```
**Ustawiane**: enforcer po wydaniu pulsa.
**Czyszczone**:
- controller po udanej re-akwizycji w fazie ACQUIRING (puls się "skonsumował").
- **lock_at** (operator porzucił dotychczasowy plan).
- **drop_to_reticle** zaczyna nowy plan, ale active_pulse zostanie nadpisany przez kolejny puls — nie trzeba clearować eksplicite.
- mode → OFF.

### Faza klatki (derived, per-frame)

```
phase(frame.t_mid_utc, active_pulse):
  None lub t_mid < issued_utc          → TRACKING
  issued_utc ≤ t_mid < motion_end_utc  → IN_FLIGHT
  motion_end_utc ≤ t_mid < settled_utc → SETTLING
  settled_utc ≤ t_mid                  → ACQUIRING
```

Faza publikowana w state jako `frame_phase` (string), UI renderuje
overlay odpowiedni do fazy.

## Regiony poszukiwań ("zielone kółko")

### Wide search (`acquired = False`)
- **Region**: koło o promieniu `wide_search_radius_px` wokół `central_point`.
- **Cel**: cold-start albo recovery po utracie.

### Narrow search (`acquired = True`)
- **TRACKING**: kwadrat `2·search_reg_px` wokół `acquired_pos`.
- **ACQUIRING**: bracket-box od `acquired_pos` do `predicted_pos`
  (axis-aligned bounding box obu z dodatkowym `search_reg_px` z każdej strony).
- **IN_FLIGHT / SETTLING**: brak search'a, brak detekcji. Owal/strzałka
  jako visualization.

### "Zielone kółko" na UI = wizualizacja narrow-search box:
- **TRACKING**: rysowane przy `acquired_pos`.
- **ACQUIRING**: przeskakuje na `predicted_pos` (gdzie solver patrzy NA TĘ KLATKĘ).
- **IN_FLIGHT / SETTLING**: zniknięte. Trajectory arrow zamiast.

## Akcje operatora i co robią

### `set_mode(mode)` (przez `set_state`)

| Z → Do | Co dzieje się z `guide_anchor` |
|---|---|
| OFF → MONITORING | Snapshot `acquired_pos` jeśli locked. Inaczej None (ustawi się przy pierwszym lockcie via `notify_acquired`). |
| OFF → GUIDING | Snapshot `acquired_pos` jeśli locked. Bez locka — None. |
| MONITORING ↔ GUIDING | **Bez zmian** w guide_anchor. Operator przełącza tryb na tej samej gwieździe. |
| → OFF | Wyczyść guide_anchor, active_pulse, predicted_pos. acquired pozostaje (nie kasujemy detekcji). |

### `lock_at(x, y)` — left-click na gwiazdę

Operator wybiera GWIAZDĘ (do śledzenia). To **nowy plan**.
- `acquired = True`
- `acquired_pos = (x, y)` (kliknięte, refined przy następnej klatce)
- `acquired_adu = None`
- `last_acquired_pos = (x, y)`, `last_acquired_adu = None`
- **`guide_anchor = (x, y)`** (jeśli mode ≠ OFF) — nowy target = ta gwiazda.
- **`active_pulse = None`**, **`predicted_pos = None`** — porzucamy poprzedni plan.

### `acquire_at(x, y)` — right-click

Operator przesuwa RETICLE (gdzie chce żeby gwiazda była).
- `central_point = (x, y)`
- **NIE rusza** acquired_pos ani guide_anchor.
- Po `acquire_at` operator typowo robi `drop_to_reticle` żeby aktywnie zacząć slewing.

### `drop_to_reticle()` — guzik

Operator mówi "wpuść tę gwiazdę do dziury (= w reticle)". Anchor staje się target.
- `guide_anchor = central_point`
- Pre-condition: mode = GUIDING i acquired = True (server enforce).
- Solver natychmiast zacznie liczyć `correction = acquired_pos − central_point` — duża, enforcer pulsy, slew.
- **`active_pulse` / `predicted_pos`**: pozostają (jeśli były) — kolejny enforcer puls je nadpisze.

## Pętla solvera per klatka

```
1. phase = classify(active_pulse, frame.t_mid_utc)
2. publish frame_phase

3. if phase ∈ {IN_FLIGHT, SETTLING}:
     notify_acquired(state.acquired, state.acquired_pos, …, frame_phase=phase)
     return None   // żadnej detekcji ani korekty

4. // TRACKING lub ACQUIRING:
   detect_full_frame() → coords, adu, candidates

5. if not state.acquired:
     return _wide(coords, adu, candidates)
   else:
     return _narrow(coords, adu, candidates, phase)
```

### `_wide` (zimny start lub odzyskanie)

1. Filtruj candidates wewnątrz wide_search_radius_px wokół central_point.
2. Jeśli pusto → `notify_acquired(False, …)` (nadal acquired=False; UI pokazuje "no candidate in wide").
3. Jeśli `last_acquired_pos` istnieje → smart-sort (proximity² + |Δadu|/last_adu). Inaczej brightest.
4. Centroid refine → `pos`.
5. `notify_acquired(True, pos, adu, recovery=last_acquired_pos is not None)`.
6. Emituj `correction = pos − (guide_anchor OR central_point)`.

**Recovery=True** = mieliśmy wcześniej fingerprint. To znak dla controllera
że to ponowne złapanie, **NIE** powod do zmiany anchora (anchor zmienia
TYLKO eksplicite operator).

### `_narrow` (mam lock, śledzę klatka-po-klatce)

1. `predicted = state.predicted_pos`
2. `pulse_pending = (predicted is not None)`
3. Region:
   - jeśli pulse_pending: bracket-box od `acquired_pos` do `predicted` z `search_reg_px` margin → faza ACQUIRING.
   - inaczej: kwadrat `2·search_reg_px` wokół `acquired_pos` → faza TRACKING.
4. Filtruj candidates w boxie.
5. Jeśli pusto: `handle_narrow_miss(pulse_pending)` (patrz niżej).
6. ADU tolerance filter (`|adu − acquired_adu| ≤ tol × exp_time`).
7. Jeśli pusto po ADU: `handle_narrow_miss(pulse_pending)`.
8. Wybierz najbliższy do `acquired_pos`, centroid refine → `new_pos`.
9. `notify_acquired(True, new_pos, new_adu)` (recovery=False).
10. `narrow_miss_count = 0`.
11. Emituj `correction = new_pos − (guide_anchor OR central_point)`.

### `handle_narrow_miss(pulse_pending)`

```
if pulse_pending: pass       # grace — puls w toku, brak detekcji oczekiwany
else: narrow_miss_count++

if narrow_miss_count ≤ 5:
    notify_acquired(True, hold_pos, hold_adu)  // republish, lock held
    return None

// budżet wyczerpany:
narrow_miss_count = 0
notify_acquired(False, None, None)             // demote do wide
```

## Enforcer (mode = GUIDING only)

```
correction = in_queue.get()
if mode ≠ GUIDING: drop (monitoring observes only, no pulse)
if cooldown active: drop

t_N, t_E = inverse_J · (correction.dx, correction.dy)
damp + clip → t_N_actual, t_E_actual

aput_pulseguide(N, t_N_actual)
aput_pulseguide(E, t_E_actual)

active_pulse = PulseEvent(
    issued_utc = now,
    motion_end_utc = now + |t_N| + |t_E|,
    settled_utc = motion_end + settle_ms,
    src_pos = state.acquired_pos,
    predicted_pos = src_pos + forward_J · (t_N_actual, t_E_actual),
    …)
state.update(active_pulse, predicted_pos = predicted)

cooldown_end_monotonic = now + |t_N| + |t_E| + settle_ms
```

## Controller `notify_acquired` (solver wywołuje)

```
update_kwargs = {acquired, acquired_pos, acquired_adu, acquired_at_ts, frame_phase}

if acquired and position:
    update_kwargs.last_acquired_pos = position
    if adu: update_kwargs.last_acquired_adu = adu
    # Prediction "się skonsumowała" — czyść:
    update_kwargs.predicted_pos = None
    update_kwargs.active_pulse = None

# Bootstrap anchor for monitoring's first lock:
if (acquired and position and not prev.acquired
    and prev.mode == MONITORING and prev.guide_anchor is None):
    update_kwargs.guide_anchor = position

# (Brak wide-recovery anchor reset — usunięte 2026-05-14:
#   abort'owało każdy świadomy slew.)

if candidates is not None:
    update_kwargs.candidates = candidates

state.update(**update_kwargs)
publish event/journal jeśli transition
```

## Cykl `guide_anchor` — gdzie się zmienia, w jednym miejscu

1. **mode → GUIDING from OFF**: snapshot `acquired_pos` (jeśli locked) lub None.
2. **mode → MONITORING from OFF**: snapshot `acquired_pos` (jeśli locked) lub None.
3. **mode → OFF**: None.
4. **lock_at(x, y)** (non-OFF): set `(x, y)`.
5. **drop_to_reticle()** (GUIDING tylko): set `central_point`.
6. **first acquire in MONITORING** (bootstrap): jeśli guide_anchor=None i acquired transitions False→True, set `position`.
7. Eksplicit caller patch `guide_anchor=...` zawsze wygrywa.

**Wszystkie inne ścieżki** (notify_acquired w guiding, wide-recovery,
solver loops): **NIE zmieniają guide_anchor**. Operator jest jedynym
źródłem zmian targetu.

## Cykl `active_pulse` / `predicted_pos`

Set:
- Enforcer po wydaniu pulsa.

Clear:
- Controller po udanej re-akwizycji (`notify_acquired(True, position, …)`).
- `lock_at`: operator porzuca poprzedni plan.
- mode → OFF.

(`drop_to_reticle` nie czyści — następny enforcer puls i tak nadpisze
active_pulse świeżą wartością.)

## "Zielone kółko" — wzorzec render-side

| Faza | Co rysuje UI |
|---|---|
| TRACKING | Box `2·search_reg_px` na `acquired_pos` (zielony dashed). |
| IN_FLIGHT | Strzałka żółta `src_pos → predicted_pos` + pomarańczowy badge "PULSING". Bez box'a. |
| SETTLING | Identycznie jak IN_FLIGHT, badge "SETTLING". |
| ACQUIRING | Box `2·search_reg_px` na `predicted_pos` (zielony dashed). Badge "ACQUIRING". |

## Audyt — co aktualny kod łamie

1. **`lock_at` nie czyści `active_pulse` / `predicted_pos`**. Jeśli
   operator klika gdy puls w toku, solver dalej myśli że jest w
   ACQUIRING, bracketuje od starego predicted_pos. Bug.

2. **`drop_to_reticle` polega na enforcerze nadpisaniu active_pulse**,
   ale jeśli enforcer ma cooldown z poprzedniego pulsa (sprzed dropa),
   puls się nie wyda dopóki cooldown nie wygaśnie. W tym oknie
   solver ma stary active_pulse (cudzy plan) + nowy guide_anchor.
   Klasyfikuje ACQUIRING wg starego predicted, bracketuje wg starego
   src_pos — szuka źle. → Też trzeba clearować przy drop.

3. **Bootstrap anchor w MONITORING** (`prev.mode == MONITORING`) ma
   subtle bug: jeśli operator startuje od GUIDING (z OFF), guide_anchor
   ustawia się przy mode-change. Ale jeśli OFF → MONITORING → GUIDING,
   anchor z monitoringu zostaje. To prawdopodobnie OK.

4. **UI status-bar pokazuje state.guide_anchor**. Jeśli lock_at
   pisze guide_anchor do state, UI powinno odzwierciedlić. Jeśli operator
   widzi że "anchor nie zmienia się przy lock_at" — sprawdzić czy:
   - lock_at faktycznie wywołuje server-side (logi),
   - state message zawiera nowy guide_anchor (NATS reader),
   - UI komponent re-renderuje (signal dependency).
