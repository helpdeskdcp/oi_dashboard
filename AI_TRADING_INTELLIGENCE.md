# BATI Trading Intelligence Platform (Milestone 10)

Status: implemented, tested, committed to `worktree-m10-trading-intelligence` only. **Not merged to `master`.** Per explicit instruction: build, test, commit to the worktree branch, wait for approval before any merge.

Mission: *"Build BATI Trading Intelligence Platform. This milestone focuses ONLY on trading intelligence, market analysis and paper trading."* Built entirely on top of, and reusing, BATI Version 1.0 (the complete autonomous framework — `agents/runtime/`, `agents/risk_manager/`, `agents/memory/`, `agents/sys_admin/`) **and** this repository's own pre-existing live option-chain engine (`oi_engine.py`, `greeks.py`, `market_structure.py`, `sr_probability_engine.py` — the exact modules `app.py`'s live dashboard and `backtest.py` both already import). No previous milestone was modified, redesigned, or rewritten.

## The central safety decision

**Every prior milestone's hardest-won rule holds here without exception: no module in `agents/trading_intelligence/` ever instantiates `app.AngelOneFetcher`, imports `SmartApi.SmartConnect`, or touches `app._shared_angel_fetcher`.**

Confirmed by direct inspection before writing a line of this milestone's code: `app.py`'s `AngelOneFetcher.__init__` performs a real SmartAPI login unconditionally, `_shared_angel_fetcher` is the ONE canonical live session the entire live app shares, and this project has a real, documented incident of a test triggering a duplicate login by touching a route that held it. A second module doing the same thing would be a second version of that exact incident, not a new kind of risk.

Every "live" read in this milestone goes through SQLite (`cycles`/`strikes`/`market_structure_snapshots` — tables `app.py`'s own live loop already writes) or the `data/history/<symbol>/3m.*` candle archives `history_engine.py` already writes — the same "read what's already been safely ingested" pattern `agents/risk_manager/data_access.py` and `agents/quant_researcher/data_access.py` established in Milestones 5–6.

**"Never place real orders. Recommendation mode and paper trading only."** Verified programmatically, not just claimed: `test_agents/trading_intelligence/test_safety.py` runs an AST scan of the entire package for any reference to the live broker session's own identifiers or an order-placement function name, and confirms `agents/trading_intelligence/` never imports `app.py` at all. `ti_store.py` — the only module that ever writes a "trade" — is verified to import nothing beyond `datetime`/`json`/`sqlite3`.

## Reuse over reimplementation

A research pass across `app.py`, `oi_engine.py`, `sr_probability_engine.py`, `market_structure.py`, and `greeks.py` (before any code was written) found that most of what this milestone asked for already exists as real, live, tested logic — reused directly rather than duplicated, honoring `oi_engine.py`'s own rule: *"Never duplicate this logic elsewhere — if live and backtest ever compute bias/signals differently, backtest results become meaningless."*

| Requested | Reused from |
|---|---|
| PCR | `oi_engine.calc_pcr` |
| Max Pain | `oi_engine.calc_max_pain` |
| OI Walls | `oi_engine.oi_walls` |
| Long/Short Build-up, Long Unwinding, Short Covering | `oi_engine.classify_buildup` (already computed per-strike, every live cycle) |
| Directional Bias | `oi_engine.detect_bias` |
| BUY CE / BUY PE / NO_TRADE, Entry/Target/SL/Confidence | `oi_engine.generate_signal` |
| Greeks (delta/gamma) | `greeks.black_scholes_greeks` |
| VWAP | `market_structure.calc_vwap` |
| Liquidity Sweep | `market_structure.detect_liquidity_sweep` (prefers the already-persisted `market_structure_snapshots.liquidity_sweep_json`) |
| Fake Breakout | `sr_probability_engine.fake_breakout_filter` (reused as a REPORTING gate here — a failed filter is the finding, not a silent block on a signal this module never places) |
| "Is this reading elevated vs. its own history" | `sr_probability_engine.compute_volume_expansion` (reused for OI-change elevation too, not just volume) |
| Win Rate / Profit Factor / Drawdown / Expectancy | `backtest.compute_advanced_trade_stats` |
| Quantity sizing | `position_sizing.compute_quantity` |
| Trade Journal | `agents.memory`'s existing `agent_memory_trade_journal` table (Milestone 4) |
| Agent Health | `agents.sys_admin.api.get_agent_status()` |

Only **Gamma Trap** and **Institutional Buying/Selling** genuinely don't exist anywhere in this repository (confirmed by search) — built as real, rule-based, honestly-labeled heuristics (the same "EXPERIMENTAL... not empirically validated" framing `oi_engine.py`'s own `compute_new_trend_meter` already uses for its own advisory additions), not proven formulas.

## Module summaries

**1. Market Data Engine** (`market_data.py` + `data_access.py`) — aggregates OI, OI change, PCR (+ change), IV, Greeks, VWAP, and volume for one symbol from already-stored `cycles`/`strikes`/candle data into one `MarketSnapshot`. "Historical storage" was not a new requirement to build — it already exists (`cycles`/`strikes`/`data/history/*`); this module's job is reading it, honestly reporting `available=False` when a symbol has never had a cycle logged rather than raising.

**2. Institutional Intelligence** (`institutional_intelligence.py`) — the full requested detection list, described above. Every finding carries real evidence (OI change, gamma value, distance to spot, etc.), never a bare label.

**2b. Strike-level AI Intelligence** (`strike_intelligence.py`) — added per the explicit follow-up request ("BATI pratyek strike sathi... OI Wall / Support-Resistance / PCR / Build-up Type / Max Pain / Expected Move / AI Buy-Sell Probability / CE-PE Strength dakhavel", matching the existing Option Chain screen). Expected Move is the standard textbook formula (`spot * IV * sqrt(T)`); AI Buy/Sell Probability is honestly documented as a POSITIONING read (derived from `oi_engine.net_oi_buildup_lean`, clamped 10–90%, never 0/100%) — explicitly distinct from `ai_trading_engine.py`'s calibrated, trade-history-based Probability, and the two are never confused with each other in the code or the UI.

**3. AI Trading Engine** (`ai_trading_engine.py`) — BUY CE / BUY PE / HOLD / NO TRADE. HOLD only exists once a position is open (checked before ever generating a fresh signal); Probability is a real historical win-rate calibration from this engine's OWN closed paper trades, bucketed by confidence, honestly `None` with a stated reason until 5+ trades exist in a bucket (never fabricated); Risk Score (0–100, documented as higher = riskier) reuses `agents.risk_manager.risk_engine.position_sizing_check`.

**4. Multi Timeframe Engine** (`multi_timeframe.py`) — a real, important finding, stated plainly rather than worked around: **this repository has only ever archived 3-minute candles.** 15m/30m/1H/Daily are real local pandas resamples of the 3m archive (clean multiples: 5/10/20/full-day). 1m is honestly reported unavailable (finer than the archived data — not recoverable). 5m is *also* honestly reported unavailable, for a less obvious reason: 5 is not a clean multiple of 3, so resampling would silently misrepresent real bar boundaries rather than just being "approximate."

**5. Paper Trading** (`paper_trading.py` + `ti_store.py`) — a new `ti_paper_trades` table (not a reuse of `app.py`'s own `paper_trades`/`scalp_paper_trades`/`v3_paper_trades` — this is a distinct engine with its own lifecycle, the same way each of those already has its own dedicated table). Every write is a pure SQLite INSERT/UPDATE with the price supplied by the caller — the exact pattern `app.py`'s own `db_open_paper_trade()` already establishes.

**6. Dashboard** (`api.py` + `templates/trading_intelligence.html` + `/admin/trading-intelligence`, `/api/trading-intelligence/overview` in `app.py`) — live option chain, OI analytics, Greeks, AI signals, risk, confidence, paper P&L, agent health, all in one admin-gated page, folded into the same Flask app as every other admin dashboard rather than a separate service.

**7. Safety** — see "the central safety decision" above.

## Real bug found and fixed this milestone

`data_access._row_to_strike_row()`'s first version passed SQLite `NULL` straight through as Python `None` for any Greeks column never written that cycle (e.g. before the IV/Greeks migration ran, or a strike Angel One's feed didn't populate). `oi_engine.StrikeRow`'s own dataclass defaults every numeric field to `0.0`, never `None` — every consumer of a `StrikeRow` in `oi_engine.py` assumes that. Found immediately by this milestone's own first integration test (`test_data_access.py::test_null_greeks_coerce_to_dataclass_defaults_not_none`), fixed by coercing `None` to each field's own dataclass default explicitly, the same per-field pattern `backtest.load_cycles()` already uses for its own `StrikeRow` construction, generalized across every field.

A second, smaller catch: `pandas` 3.0 rejects the frequency strings `"15m"`/`"1H"` outright (`ValueError: 'm' is no longer supported for offsets`) — `multi_timeframe.py`'s resample rules use the pandas-3.x-correct `"15min"`/`"30min"`/`"1h"` instead, caught before this module ever shipped by testing the resample against the real archive rather than assuming the naive frequency string would work.

## Database schema

```sql
ti_paper_trades   -- this engine's own paper trades (NEW table, not shared with app.py's other engines)
ti_signal_log     -- every AI Trading Engine recommendation, including NO_TRADE/HOLD, for explainability
```

Both in `oi_history.db`, indexed from the start, `PRAGMA busy_timeout=5000` on every connection — matching every prior milestone's own store convention. No new tables were added to `cycles`/`strikes`/`market_structure_snapshots` — those stay entirely owned by `app.py`.

## Test summary

83 tests across `test_agents/trading_intelligence/` (10 files: `test_data_access.py`, `test_market_data.py`, `test_institutional_intelligence.py`, `test_strike_intelligence.py`, `test_ai_trading_engine.py`, `test_multi_timeframe.py`, `test_ti_store.py`, `test_paper_trading.py`, `test_api.py`, `test_safety.py`), including a real 9-strike synthetic option chain fixture (`insert_realistic_chain` — large enough that `oi_engine.oi_walls()`'s top-3 logic behaves the same way it does against a real live chain, unlike a toy 2-3-strike chain where every row is trivially a "wall"), real OHLC-aggregation verification against actual archived NIFTY candles, and a full HOLD → target-hit → auto-close → journal round trip.

`agents/trading_intelligence/`: 10 files, 1,415 lines. `test_agents/trading_intelligence/`: 12 files, 863 lines.

Full repository suite: **1,183 passed, 1 xfailed** (up from 1,100 pre-Milestone-10 — exactly the 83 new tests), zero regressions.

## Known, documented limitations

- **No live `oi_history.db` exists in this dev/CI environment** (consistent with every prior milestone's own note) — every test here builds its own realistic synthetic schema. Everything in this milestone reads the exact same tables `app.py`'s live loop writes in production; nothing about the code path differs between "synthetic test data" and "real live data."
- **1m and 5m candle timeframes are not available**, honestly, for the reasons in Module 4's own section above — closing this gap would require a genuine historical fetch from Angel One via `history_engine.py`, out of scope for a module that reads what's already archived (and would need the shared live session anyway).
- **Gamma Trap and Institutional Buying/Selling are new, advisory-only heuristics**, not validated against real historical outcomes the way `oi_engine.py`'s core signal logic has been through years of live use — flagged as such in their own docstrings, same as `oi_engine.compute_new_trend_meter`'s own honesty precedent.
- **AI Buy/Sell Probability (per-strike, Module 2b) and Probability (AI Trading Engine, Module 3) are deliberately different numbers** computed from different evidence (OI positioning lean vs. historical trade calibration) — documented explicitly in `strike_intelligence.py`'s own module docstring to prevent the two ever being conflated in a future reader's mind.
- **Nothing in this milestone is wired into the Milestone 9 runtime scheduler yet** — `ai_trading_engine.evaluate()`/`paper_trading.enter_from_recommendation()` are real, callable, tested functions, but no `agents/runtime/agent_runtime.py` cycle invokes them automatically. Wiring a seventh scheduled cycle in is a natural, small follow-up, deliberately left out of this milestone's own scope ("focuses ONLY on trading intelligence, market analysis and paper trading").
