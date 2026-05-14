# Guider — architectural review and feature roadmap (2026-05-14)

Gospodarski review po pierwszych tygodniach produkcji. Operator wskazał
że potrzebujemy: (a) robustness — pipeline nie może zawisać tak, że
operator musi się logować i restartować, (b) lista nowych funkcji
poniżej, (c) pomysł na uproszczenie. Ten dokument robi audyt i
proponuje plan; konkretne zmiany kodu wiszą jako follow-up commits.

## Stan zastany — krótkie streszczenie

Pipeline składa się z czterech stages spiętych asyncio.Queue-ami:

```
camera_array_collector  →  stacker  →  solver  →  enforcer
       (RawFrame)         (passthru)   (Correction)  (pulses)
                              ↓
                       thumbnail_emitter
                              (JPEG + NATS notify)
```

Kontroler arbitruje state (single writer), wszyscy konsumenci czytają
przez `state.snapshot()`. NATS gada przez `nats_conn.py`:
- `svc.publish.guider.<inst>.pipeline.<pipe>.state` — pełen state na
  każdą mutację (~kilka Hz)
- `svc.publish.guider.<inst>.pipeline.<pipe>.events` — pulse, mode
  transitions, journals
- `svc.publish.guider.<inst>.<inst>.frame.thumbnail.ready` — notyf JPEG
- `svc.publish.guider.<inst>.rpc.<command>` — operator commands

UI (Angular 21, signals, OnPush) słucha state/events/thumbnail-ready,
robi optymistyczne RPC dla mutacji.

## Co działa dobrze

- **Stage-oriented pipeline z queue-ami** — orthogonalne, łatwo
  dodać stage (np. FITS writer). Bez tego nie dałoby się robić
  ulepszeń bez przepisywania monolitu.
- **Single-writer state + snapshot-read** — żadnych race'ów,
  RPC są punktem prawdy operator-side, solver-side pisze tylko
  przez `notify_acquired`.
- **NATS-first design** — UI nigdy nie polluje serwisu, wszystko leci
  przez subjecty, dyskoverowalne. tcsctl / operator / Halina mogą
  podglądać identycznie.
- **Forward + inverse Jacobian, auto_adjust adapter** — czysty
  matematyczny layer w `pulse_guide.py`. Łatwo wymienić na adaptive
  (RLS / GP) bez ruszania reszty.
- **Phase 1 (PulseEvent)** — pierwszy explicit temporal model po
  poprzedniej iteratywnie-zalataywanej historii.

## Co boli — uporządkowane

### 1. Pipeline może zawisnąć permanentnie (CRITICAL)

Każdy `await` w łańcuchu może zablokować się indefinitely:
- `backend.submit_one` → `protocol.fetch` → `_wait_image_ready`
  ma deadline `exp_time + max(10, exp_time)` — OK,
- `_fetch_bytes` ma `request_timeout=30` — OK,
- ALE: `enforcer.in_queue.put(correction)` w monitoring mode nigdy nie
  drenuje (enforcer skip'uje _apply ale konsumuje), więc to OK,
- jednak `await self._notify_acquired(...)` przy locked `state._lock`
  trzymanym przez RPC handler może zawisnąć — TODO sprawdzić,
- `aput_pulseguide` na mount-hangu MIMO timeoutu w transport — TBD.

**Symptom widziany:** thumbnaile przestały lecieć ~26 min po starcie,
sequence zatrzymał się przy 1094. Status DEGRADED wykrywa to po
`last_cycle_age_s > 30`, ale to tylko sygnalizacja — recovery wymaga
restartu service'u przez operatora.

**Plan (Phase 4 — bounded waits everywhere):**
- `asyncio.wait_for(backend.submit_one(...), timeout=max(3 * exp_time, 10))`
  w camera collector. Na TimeoutError: log warning, skip frame, kontynuuj.
- Drugi `wait_for` wokół jednego pełnego cyklu solvera (FFS może mieć
  pathology na konkretnym kadrze).
- **Self-healing watchdog** w manager: jeśli `last_cycle_age_s > 60`,
  manager *automatycznie* re-inicjalizuje pipeline (close protocol,
  reopen, restart stage tasks) bez restartu całego service'u.
  Operator widzi w journalu "auto-recovery: pipeline X re-initialized
  after 60s stall" i nic nie musi robić.
- Drop-oldest queue policy (już mamy w `camera_array_collector`)
  rozszerzona na **stacker→solver**: jeśli kilka klatek w kolejce,
  solver zawsze bierze najnowszą — stare są bezużyteczne real-time.

### 2. Pole `state` rośnie i blendzi role (MAJOR)

`PipelineState` ma ~30 pól w trzech kategoriach (operator/auto/observed)
w jednym dataclass. Mieszanie operator-config z runtime-observed:
- Komplikuje "operator zmienił ustawienie" vs "solver się odświeżył"
  semantykę.
- Powoduje że każde `set_state` z UI propaguje też observed fields
  z powrotem (no-op po stronie semantyki, ale szumi w state messages).
- UI nie ma jasnego sposobu pokazać "tu jest pending change, tu jest
  current" (operator's UX feedback).

**Plan (Phase 5 — split state):**
- `PipelineConfig` — operator-controlled fields. RPC mutates this.
- `PipelineRuntime` — auto + observed. Solver/enforcer write.
- State message: `{ config: {...}, runtime: {...}, version: N }`.
  UI łatwo robi "current config" vs "pending patch" diff.

### 3. Method = string, hardcoded (MEDIUM)

`state.method = "single_star"` jest sprawdzane w solverze przez:
```python
if state.method == "single_star": …
```
Trzeba dodać nowe metody (multi-star, fiber-hole, ewentualnie
plate-solve dla cold-start). Brak punktu rozszerzenia.

**Plan (Phase 6 — method registry):**
```python
@register_method("single_star")
class SingleStarMethod:
    async def iterate(frame, state) -> Correction | None: ...

@register_method("fiber_hole")
class FiberHoleMethod: ...  # uses fiber_radius_px from state
```
Solver instancjuje klasę po nazwie. Każda metoda dostaje config przez
`state.method_params` (już mamy). UI dropdown driven by
`available_methods` w state.

### 4. Calibration jako collapsible (MINOR, UI)

Operator słusznie zauważa: "rozsuwanie sugeruje że to czynność częsta".
Calibration jest rzadkim trybem — wejście-wyjście, nie tygodniowy
panel. Modal lub right-side replace MANUAL PULSE pasuje lepiej.

### 5. Apply pattern bez current vs pending diff (MINOR, UI)

Suwak pokazuje pending value, nie current (server-confirmed) value.
Operator nie wie czy `exp_time=2.0` to "co właśnie zmieniłem ale nie
applnąłem" czy "co aktualnie działa". Już mamy state messages od serwera —
trzeba je pokazać.

## Future features — TODO consolidated

Wszystkie z dzisiejszej rozmowy + parking lot od wcześniej, w jednym
miejscu. Każdy punkt z oszacowaniem effort i miejscem w architekturze.

### Backend (`ocabox-tcs/guiding_svc`)

| # | Feature | Touches | Effort | Notes |
|---|---|---|---|---|
| B1 | Bounded waits + auto-recovery watchdog | collector, manager | M | Phase 4. Highest priority — fixes "guider się zawiesza". |
| B2 | Method registry (multi-star, fiber-hole) | solver/methods/, state | M | Phase 6. Fiber-hole = `correction -= fiber_radius_px` w direction of move kiedy gwiazda WCHODZI w dziurę (nie reaguj na "zniknęła w dziurze"). |
| B3 | Exclusion zones | state, single_star._filter | S | List of rectangles w state. Solver odsiewa detekcje wewnątrz. UI rysuje. |
| B4 | Real stacking (none / mean / median / darks) | stacker | M | Stacker dziś passthrough. CalibrationConfig już istnieje, trzeba dorobić read-darks-from-disk. |
| B5 | Auto-shutoff UT | manager, state | S | `auto_shutoff_ut: "06:00"`. Manager timer ustawia wszystkie pipelines OFF. |
| B6 | FITS snapshot (manual + every Ns) | new stage `fits_writer.py` | M | Tap analysis queue. Naming: `jk15g_YYYYMMDD_HHMMSS_<seq>.fits`. Headers: TIC observatory + camera + guider state. PSP downloader kod można pożyczyć. |
| B7 | Camera temperature monitoring | tic_conn poll, state.observed | S | Już w TIC live document, trzeba surface do `state.runtime.camera_temp_c`. |
| B8 | Tracking-from-NATS indicator | nats_conn subscriber on TIC mount, state.observed | S | `tic.mount.<scope>.tracking` → `state.runtime.mount_tracking_on`. Solver może też reagować (lock loss jeśli się wyłączy). |
| B9 | Phase 2 timing rework (drop-to-reticle fix) | solver, controller | M | Z handoff doc, plan w `SESSION_HANDOFF_2026-05-09.md`. |
| B10 | State split (config / runtime) | state, controller, all consumers | L | Phase 5. Większy refactor, ale unraveling — robi wiele rzeczy łatwiejszymi. |

### UI (`ocabox-guider-ui`)

| # | Feature | Touches | Effort | Notes |
|---|---|---|---|---|
| U1 | Apply: current vs pending diff display | guider-dashboard, settings panels | M | Slider shows pending; small ⤳ + current value next to it; clear gdy server echoes back. |
| U2 | Method dropdown + per-method config | mode-toolbar / new method-panel | S | Driven by B2. |
| U3 | Subraster presets + custom panel | new component, state.method_params | S | Predefined sizes wokół reticle; custom dla power user. |
| U4 | Exclusion zone drawing tool | frame-view SVG | M | Toggle button → rysuj prostokąty na klatce, czerwona kreska. Reset clear. Driven by B3. |
| U5 | Stacking mode selector | settings panel | S | Driven by B4. |
| U6 | Auto-shutoff UT input | settings panel | S | Driven by B5. |
| U7 | FITS snapshot controls (manual + auto) | new fits-panel | S | Driven by B6. |
| U8 | Thumbnail-resolution control | settings panel | S | State already has `thumbnails.size`. Just expose. |
| U9 | Calibration as modal/right-side panel | dashboard layout, calib component | S | Replaces MANUAL PULSE when active. |
| U10 | Camera temperature display | status bar | S | Driven by B7. |
| U11 | Tracking indicator (with alert when off) | status bar | S | Driven by B8. |
| U12 | Phase 3 timing visualization (oval + status pill) | frame-view, drift-chart | M | Driven by B9. |
| U13 | Frame timestamp on image | frame-view overlay | done | Already in main bundle. |

### Cross-cutting (both repos)

| # | Feature | Touches | Effort | Notes |
|---|---|---|---|---|
| X1 | Phase 4 bounded waits + watchdog | manager, collector, UI status reflect | M | Critical reliability. **Should be next.** |
| X2 | Phase 5 state split | state.py, store.ts, all settings panels | L | Major refactor but enables clean apply UX (U1) and makes future features less invasive. |

## Czy architektura jest dobra — odpowiedź

**Tak, fundament jest zdrowy.** Stages + queues + single-writer state +
NATS-first to dobry wybór dla real-time guidera. Niczego z grubsza nie
trzeba przepisywać. Co potrzebujemy:

1. **Bounded waits + auto-recovery (B1/X1)** — pojedyncza zmiana z
   największym beneficyjnym wpływem na operator UX. Eliminuje
   "guider się zawiesił, zaloguj się i zrestartuj".
2. **State split (X2)** — porządkuje to co już chcemy zrobić w UI
   (U1) i ułatwia każdą kolejną feature.
3. **Method registry (B2)** — daje punkt rozszerzenia dla nowych
   trybów (multi-star, fiber-hole) bez gmerania w solverze.

Wszystkie pozostałe features (B3-B8, U1-U13) są **inkrementalne** na
tej bazie — wpadają w istniejące dataclass'y, queue'y, NATS subjecty.

## Proponowana kolejność robót

Faza A (krytyczne reliability) — 1 commit każde:
1. **X1 / B1**: bounded waits + auto-recovery watchdog. Pierwszy
   priorytet, eliminuje hang-and-restart cycle.
2. **B9 / Phase 2 timing**: faktyczny fix drop-to-reticle.
   Już mamy Phase 1 deployed; Phase 2 to solver-side consumer
   + clear z `predicted_pos`.

Faza B (operator-facing, niewielkie zmiany backend):
3. **B3 + U4**: exclusion zones. Krytyczne dla operatora żeby
   nie przeskakiwać na zakłócające obiekty.
4. **B5 + U6**: auto-shutoff UT. Operator nie musi pamiętać.
5. **B7 + U10**: camera temperature.
6. **B8 + U11**: tracking indicator.

Faza C (UX cleanup):
7. **U9**: calibration jako modal.
8. **X2 + U1**: state split + current vs pending diff.

Faza D (większe features):
9. **B2 + U2**: method registry + dropdown.
10. **B4 + U5**: real stacking.
11. **B6 + U7**: FITS snapshot.
12. **B9 finishing**: Phase 3 GUI timing visualization (oval, status pill).

## Phase 4 — robustness by design (NOT watchdog)

Operator's directive (2026-05-14): nie "watchdog ratujący skutki", ale
**eliminacja źródeł hangu by design**. Pipeline po prostu nie może się
zawiesić bo każdy zewnętrzny await ma bounded timeout, a każda kolejka
jest drop-oldest. Wszystkie problemy są naturalnym stanem działania,
elegancko obsłużonym.

### Zasady

1. **Każdy `await` na zewnętrzne IO ma explicit `asyncio.wait_for`
   z timeoutem proporcjonalnym do exp_time (lub stałym).**
   - Camera fetch: `3 × exp_time + 15 s` (od `submit_one` całość).
   - Mount aput_pulseguide: `5 s` (ASCOM fire-and-forget = ms-rząd
     w zdrowym świecie; 5 s to obrona przed stuck TIC handler).
   - NATS publisher: `2 s` (LAN < 50 ms; 2 s to broker blip ceiling).
   - FFS detection (in to_thread): `5 × exp_time` (pathological frame).

2. **Każda kolejka w hot-path = drop-oldest, producer NEVER blocks.**
   - Camera → Stacker: drop-oldest ✓ (już było).
   - Stacker → Solver: drop-oldest ✓ (dodane).
   - Solver → Enforcer: drop-oldest ✓ (dodane). Real-time:
     stała korekta sprzed N klatek = bezużyteczna; latest wins.
   - Stacker → ThumbnailEmitter (tap): drop-oldest ✓ (już było).

3. **NATS publish failures są non-fatal.**
   - Timeout / brokerError → log warning, skip wiadomość, pipeline jedzie.
   - State self-healing: następna mutacja republikuje pełen stan.

4. **State lock trzymany tylko nad dict-assignments, nigdy nad I/O.**
   - Audit: `update()` w `state.py` — tylko setattr pod lock, OK.

5. **Komunikaty log są szczere co do niewiedzy.**
   - "another Alpaca client may be stealing" → wymieniona na listę
     możliwych przyczyn z honest framing.

### Stages — czego dotyka jeden commit

- `stages/solver/base.py`: bounded FFS via `wait_for`, drop-oldest na
  out_queue, log warning na drop.
- `stages/stacker.py`: drop-oldest na primary out_queue.
- `stages/enforcer.py`: `wait_for` wokół obu `aput_pulseguide`. Na
  timeout: skip cooldown update (next cycle re-attempt), brak hangu.
- `controller.py`: każdy `_publish_*` opakowany `wait_for(2s)`. Manual
  pulse `aput_pulseguide` z `wait_for(5s)`.
- `camera_array_collector.py`: top-level `wait_for(submit_one)` z
  `3 × exp_time + 15s` deadline. Pokrywa wszystkie ocabox-API calls
  które nie mają wewnętrznych timeoutów (`aput_binx`, `aput_gain`,
  `aput_startexposure`).
- `protocols/alpaca.py`: honest warning message.

### Czego TO NIE załatwia (i dlaczego OK)

- **TIC po stronie obs01 mogący wisieć**: jeśli aput_pulseguide
  faktycznie nie wraca po 5 s, robimy log + skip pulse. Następny
  cykl spróbuje ponownie. Operator zobaczy w journalu częstotliwość
  timeoutów — to sygnał diagnostyczny, nie awaria.
- **Camera reaguje powolnie raz na 100 ramek**: 3×exp_time + 15 s
  zniwleluje normalną wolność. Patologiczne zwisy ucinamy.
- **NATS broker reboot**: publish timeouty kilka sekund, potem
  re-connect. State self-healing.

### Czego TO NIE robi (deliberately, per operator)

- ~~Manager watchdog auto-recovery~~. Wycofane — eliminujemy źródła,
  nie maskujemy. Jeśli mimo wszystko coś się zawiesi (bug nie ujęty
  w bounds), kara naturalna: operator zauważy DEGRADED status w UI
  i zbada przyczynę. Robustness-by-design > recovery-after-fact.

Estymata: 1 focused commit. ~150 LOC zmian rozproszonych po stages.

## Note on "drugi klient ukradł sygnał"

Operator słusznie zwrócił uwagę — bez świadomej drugiej instancji to
sformułowanie myli. Faktyczna obserwacja w logach to log z
`alpaca.py:200`:

> `_wait_image_ready: imageready never True but camerastate
> Exposing→Idle — another Alpaca client may be stealing the signal.`

Komentarz w kodzie zakłada że "imageready false + state idle" = drugi
klient. Ale to może być też:
- Glich firmware'u kamery,
- Reset sieciowy podczas ekspozycji,
- Race condition w ASCOM driverze.

Komunikat warning sugeruje pewną przyczynę kiedy nie wiemy. Lepiej:

> `_wait_image_ready: camerastate transitioned Exposing→Idle without
> raising imageready (could be camera firmware quirk, network glitch
> during exposure, or another Alpaca client). Fetching anyway; if
> bytes are stale, the framing-watchdog will recover.`

Zmiana w komunikacie + nadzieja że Phase 4 watchdog faktycznie
recoverује. Cleanup do tej iteracji.
