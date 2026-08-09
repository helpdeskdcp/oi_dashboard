# Milestone 13 — Phase 1: Intelligence Orchestrator

**Status:** Implemented, tested, committed. **NOT merged, NOT deployed, scheduler remains disabled.**

## File location discrepancy (flagged before writing any code)

The brief requested:

- `backend/engines/{probability,oi,volume,greeks,mathematics}_engine.py` (as pre-existing engines to wrap)
- `backend/runtime/intelligence_orchestrator.py`
- `backend/runtime/models.py`
- `backend/tests/runtime/test_intelligence_orchestrator.py`

No `backend/` directory and no `tests/` (or `backend/tests/`) directory exists anywhere in this repository. Every standalone/cross-cutting module lives at the repo root (`oi_engine.py`, `greeks.py`, `sr_probability_engine.py`, `mcx_session_config.py`, `shadow_mode_cli.py`, ...), and every test file is `test_*.py` at the repo root. This exact discrepancy has already been flagged and accepted twice before this session (`mcx_session_config.py`, `test_shadow_mode_cli.py`). Applied the same resolution:

| Requested | Actual |
|---|---|
| `backend/runtime/intelligence_orchestrator.py` | `intelligence_orchestrator.py` (repo root) |
| `backend/runtime/models.py` | `intelligence_models.py` (repo root — not bare `models.py`, to avoid being the first generic single-word module name among 30+ specifically-named top-level modules) |
| `backend/tests/runtime/test_intelligence_orchestrator.py` | `test_intelligence_orchestrator.py` (repo root) |

Of the five named engines, only `oi_engine.py` is real. The other four map to real modules/derivations as described below — no file was fabricated to match a nonexistent name.

## Files Created

- **`intelligence_models.py`** (40 lines) — the `MarketIntelligenceSnapshot` dataclass, exactly as specified in the brief, plus a `to_dict()` helper.
- **`intelligence_orchestrator.py`** (236 lines) — `build_snapshot(symbol, *, timeframe="3m") -> MarketIntelligenceSnapshot | None`, the orchestrator's one entrypoint.
- **`test_intelligence_orchestrator.py`** (278 lines, 27 tests) — engine-adapter, bias-resolution, determinism, read-only, and API-route coverage.

## Files Modified

- **`app.py`** (+20 lines): one new import (`intelligence_orchestrator`) and one new route, `GET /api/intelligence/snapshot`, inserted immediately after the Shadow Mode routes block. No other line in `app.py` touched.

## Architecture

```
GET /api/intelligence/snapshot?symbol=NIFTY
        │
        ▼
intelligence_orchestrator.build_snapshot(symbol)
        │
        ├─ agents.trading_intelligence.market_data.get_snapshot(symbol)   (real, already-stored cycle/strike data)
        ├─ agents.trading_intelligence.data_access.load_candles(symbol)   (real, already-archived candles)
        ├─ agents.trading_intelligence.data_access.latest_market_structure(symbol)
        │
        ├─ oi_engine.detect_bias() / oi_walls() / generate_signal() / compute_trend_meter()
        │        → confidence, oi_strength, bias (normalized), greeks_alignment
        │
        ├─ _volume_and_liquidity()  (real ce_vol/pe_vol summed — no dedicated "volume engine" module exists)
        │        → volume_score, liquidity_score
        │
        ├─ sr_probability_engine.compute_institutional_entry_score()
        │        → institutional_score
        │
        └─ statistics.mean([oi_strength, institutional_score, confidence])  (this module's own "mathematics" layer)
                 → probability_score
        │
        ▼
MarketIntelligenceSnapshot(...)  or  None (no cycle data yet for this symbol)
```

### Engine mapping (brief's names → what actually exists)

| Brief's name | Real source | Notes |
|---|---|---|
| `oi_engine.py` | `oi_engine.py` (repo root) | Real, used directly: `detect_bias`, `oi_walls`, `generate_signal`, `compute_trend_meter`, `BIAS_LEAN` |
| `probability_engine.py` | `sr_probability_engine.py` | `compute_institutional_entry_score()` — also backs `institutional_score` |
| `volume_engine.py` | *(none exists)* | Derived from real `ce_vol`/`pe_vol` already on each strike row against a documented `LIQUID_VOLUME_REFERENCE = 50_000` constant |
| `greeks_engine.py` | *(none exists as such)* | `greeks.py`'s `black_scholes_greeks()` is real but needs an expiry date this read-only layer doesn't have without importing the broker-facing `AngelOneFetcher`. Reuses `generate_signal()`'s own already-resolved `delta_used`/`direction` instead of a second, independent calculation |
| `mathematics_engine.py` | *(none exists)* | This orchestrator's own deterministic `statistics.mean(...)` aggregation step is the "mathematics" layer — documented, not fabricated |

### Field-by-field data lineage

| Field | Source |
|---|---|
| `bias` | `compute_trend_meter()`'s 7-value "zone" collapsed to BULLISH/BEARISH/NEUTRAL |
| `confidence` | `compute_trend_meter()`'s own `score` (0-100) |
| `oi_strength` | `generate_signal()`'s own `confidence` (0-100) |
| `volume_score` | real `ce_vol`+`pe_vol` vs. `LIQUID_VOLUME_REFERENCE`, capped at 100 |
| `greeks_alignment` | `generate_signal()`'s own `delta_used`/`direction` (CE→"BULLISH LEAN", PE→"BEARISH LEAN", missing delta→"UNAVAILABLE") |
| `institutional_score` | `sr_probability_engine.compute_institutional_entry_score()`, fed real derived inputs (VWAP alignment, regime, dual-source/wall-cross flags from the signal, liquidity score); `price_structure` honestly left `None` (documented Phase 1 gap — no swing-structure classifier exists yet) |
| `probability_score` | `round(mean([oi_strength, institutional_score, confidence]))` — an explicit cross-engine consensus, not a single engine's own statistical model |

## Safety

- **Pure read-only.** The only functions called anywhere are `market_data.get_snapshot()`, `data_access.load_candles()`/`latest_market_structure()` (all already-stored-data readers), and `oi_engine.py`/`sr_probability_engine.py`'s pure functions. `ai_trading_engine.evaluate()` is deliberately never called (it has a real side effect — closing open `ti_paper_trades` positions — the same reason Shadow Mode's `observer.py` avoids it).
- **No scheduler wiring.** `build_snapshot()` is called only by the new route and by tests. `RUNTIME_SCHEDULER_ENABLED` and `RUNTIME_CONTROL_API_ENABLED` are untouched (still default `False`). `agents/runtime/scheduler.py` and `agents/runtime/lifecycle.py` have zero diff. `NEVER_SCHEDULABLE_AGENTS` is unchanged (`trading_intelligence`, `quant_researcher`, `shadow_mode`) — this module doesn't register a runtime agent at all.
- **No broker/Angel One integration.** No import of `AngelOneFetcher` or any `smartapi`/broker module anywhere in `intelligence_orchestrator.py`.
- **No background tasks.** No thread, no timer, no autostart hook.
- **Deterministic.** Every score is a documented arithmetic function of already-stored inputs; calling `build_snapshot()` twice on identical data returns an identical result (asserted directly in tests).
- **Honest degradation.** Returns `None` (route returns `404`) rather than a fabricated snapshot when no market data has been logged yet for a symbol — same contract `agents/shadow_mode/observer.py`'s `observe_and_predict()` already established.

## API

`GET /api/intelligence/snapshot?symbol=NIFTY` — admin-gated (`@auth.roles_required("admin")`), GET-only (no `methods=` argument, so POST/PUT/DELETE 405 automatically). Missing `symbol` → `400`. No data for the symbol → `404` with an honest error message. Otherwise `200` with the snapshot.

### Sample response (real, generated via the Flask test client against a seeded 9-strike NIFTY chain — not hand-written)

```json
GET /api/intelligence/snapshot?symbol=NIFTY  →  200

{
  "symbol": "NIFTY",
  "bias": "NEUTRAL",
  "confidence": 48,
  "oi_strength": 0,
  "probability_score": 18,
  "volume_score": 100,
  "greeks_alignment": "UNAVAILABLE",
  "institutional_score": 5
}
```

(This particular sample used a flat, symmetric synthetic chain — CE/PE OI, volume, and price identical at every strike — which is why the engines correctly resolve to NEUTRAL/UNAVAILABLE; a directionally-skewed chain produces a directional `bias`/`greeks_alignment`, exercised in `test_intelligence_orchestrator.py`'s adapter-level tests.)

## Test Results

```
$ python3 -m pytest test_intelligence_orchestrator.py -q
27 passed in 5.14s

$ python3 -m pytest -q          # full repo suite
1590 passed, 1 xfailed in 312.37s (0:05:12)
```

The single `xfailed` is a pre-existing, unrelated expected-failure elsewhere in the suite (present before this change).

Coverage in `test_intelligence_orchestrator.py`:
- Engine adapters return normalized 0-100 scores (`TestEngineAdaptersProduceNormalizedScores`) — includes volume score scaling and capping behavior.
- Bias resolution across all three states, both at the adapter level (`_normalize_bias` over the real 7-zone vocabulary) and end-to-end (`TestBiasResolution`).
- Greeks-alignment adapter for CE/PE/missing-delta/no-direction (`TestGreeksAlignmentAdapter`).
- Deterministic aggregation — identical inputs twice → identical output; `probability_score` verified equal to the documented mean formula (`TestDeterministicAggregation`).
- Honest `None`/`404` degradation with no data or an unknown symbol (`TestNoDataDegradesHonestly`).
- Zero DB writes across repeated calls (`TestPureReadOnly`).
- Route behavior: 400 (no symbol), 404 (no data), 200 (real data, full field shape), 405 (POST), 302 (unauthenticated), 403 (non-admin) (`TestSnapshotRoute`).

## Confirmation

- `RUNTIME_SCHEDULER_ENABLED` = `False` (unchanged, `agents/config.py`).
- `RUNTIME_CONTROL_API_ENABLED` = `False` (unchanged, `agents/config.py`).
- `NEVER_SCHEDULABLE_AGENTS` = `{"trading_intelligence", "quant_researcher", "shadow_mode"}` (unchanged).
- `agents/runtime/scheduler.py`, `agents/runtime/lifecycle.py`: zero diff.
- No broker/Angel One import anywhere in the new files.
- No new database tables, no background thread, no scheduler registration.
- **Not merged to `master`. Not deployed. Not restarted on any running process.**

## Commit

`90b0ea6` — "Milestone 13 Phase 1: Intelligence Orchestrator" (worktree `m13-phase1-intelligence-orchestrator`, branch `worktree-m13-phase1-intelligence-orchestrator`, based on `master@f254aac`).

Awaiting explicit instruction before any merge/deploy step, per this phase's own safety constraints and this project's established convention.
