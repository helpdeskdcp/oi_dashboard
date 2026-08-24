# BATI Trading Intelligence Platform (Milestone 10)

Status: implemented, deep-reviewed and hardened, tested, committed to `worktree-m10-trading-intelligence` only. **Not merged to `master`.** Per explicit instruction: build, review to institutional-grade quality, test, commit to the worktree branch, wait for approval before any merge.

Mission: *"Build BATI Trading Intelligence Platform. This milestone focuses ONLY on trading intelligence, market analysis and paper trading."* Built entirely on top of, and reusing, BATI Version 1.0 (the complete autonomous framework — `agents/runtime/`, `agents/risk_manager/`, `agents/memory/`, `agents/sys_admin/`) **and** this repository's own pre-existing live option-chain engine (`oi_engine.py`, `greeks.py`, `market_structure.py`, `sr_probability_engine.py` — the exact modules `app.py`'s live dashboard and `backtest.py` both already import). No previous milestone was modified, redesigned, or rewritten.

This document covers two passes: the original build, and a subsequent **institutional-grade review pass** (removing duplicated work, enriching every recommendation and strike-level output, and a dedicated validation suite) requested before merge approval. Both are reflected below as the current state of the code — see "Review pass: what changed and why" for what specifically was added on top of the original build.

## The central safety decision

**Every prior milestone's hardest-won rule holds here without exception: no module in `agents/trading_intelligence/` ever instantiates `app.AngelOneFetcher`, imports `SmartApi.SmartConnect`, or touches `app._shared_angel_fetcher`.**

Confirmed by direct inspection before writing a line of this milestone's code: `app.py`'s `AngelOneFetcher.__init__` performs a real SmartAPI login unconditionally, `_shared_angel_fetcher` is the ONE canonical live session the entire live app shares, and this project has a real, documented incident of a test triggering a duplicate login by touching a route that held it. A second module doing the same thing would be a second version of that exact incident, not a new kind of risk.

Every "live" read in this milestone goes through SQLite (`cycles`/`strikes`/`market_structure_snapshots` — tables `app.py`'s own live loop already writes) or the `data/history/<symbol>/3m.*` candle archives `history_engine.py` already writes — the same "read what's already been safely ingested" pattern `agents/risk_manager/data_access.py` and `agents/quant_researcher/data_access.py` established in Milestones 5–6.

**"Never place real orders. Recommendation mode and paper trading only."** Verified programmatically, not just claimed: `test_agents/trading_intelligence/test_safety.py` runs an AST scan of the entire package for any reference to the live broker session's own identifiers or an order-placement function name, and confirms `agents/trading_intelligence/` never imports `app.py` at all. `ti_store.py` — the only module that ever writes a "trade" — is verified to import nothing beyond `datetime`/`json`/`sqlite3`. This scan, and the rest of the safety contract, were re-verified (unchanged) after the review pass.

## Reuse over reimplementation

A research pass across `app.py`, `oi_engine.py`, `sr_probability_engine.py`, `market_structure.py`, and `greeks.py` (before any code was written) found that most of what this milestone asked for already exists as real, live, tested logic — reused directly rather than duplicated, honoring `oi_engine.py`'s own rule: *"Never duplicate this logic elsewhere — if live and backtest ever compute bias/signals differently, backtest results become meaningless."*

| Requested | Reused from |
|---|---|
| PCR | `oi_engine.calc_pcr` |
| Max Pain | `oi_engine.calc_max_pain` |
| OI Walls | `oi_engine.oi_walls` |
| Long/Short Build-up, Long Unwinding, Short Covering | `oi_engine.classify_buildup` (already computed per-strike, every live cycle) |
| Directional Bias | `oi_engine.detect_bias` |
| BUY CE / BUY PE / NO_TRADE, Entry/Target1/SL/Confidence | `oi_engine.generate_signal` |
| Greeks (delta/gamma) + Probability of ITM | `greeks.black_scholes_greeks` (extended this review pass — see below) |
| VWAP | `market_structure.calc_vwap` |
| Liquidity Sweep | `market_structure.detect_liquidity_sweep` (prefers the already-persisted `market_structure_snapshots.liquidity_sweep_json`) |
| Fake Breakout | `sr_probability_engine.fake_breakout_filter` (reused as a REPORTING gate here — a failed filter is the finding, not a silent block on a signal this module never places) |
| Premium Momentum | `sr_probability_engine.compute_premium_momentum` (reused this review pass for both the AI Trading Engine's evidence and Strike Intelligence's own per-strike field) |
| "Is this reading elevated vs. its own history" | `sr_probability_engine.compute_volume_expansion` (reused for OI-change elevation too, not just volume) |
| Win Rate / Profit Factor / Drawdown / Expectancy | `backtest.compute_advanced_trade_stats` |
| Quantity sizing | `position_sizing.compute_quantity` |
| Trade Journal | `agents.memory`'s existing `agent_memory_trade_journal` table (Milestone 4) |
| Agent Health | `agents.sys_admin.api.get_agent_status()` |

Only **Gamma Trap** and **Institutional Buying/Selling** genuinely don't exist anywhere in this repository (confirmed by search) — built as real, rule-based, honestly-labeled heuristics (the same "EXPERIMENTAL... not empirically validated" framing `oi_engine.py`'s own `compute_new_trend_meter` already uses for its own advisory additions), not proven formulas.

## Architecture & data flow

```
                     ┌─────────────────────────────────────────────┐
                     │   app.py live loop / history_engine.py      │
                     │   (the ONLY code that ever touches the      │
                     │    Angel One session -- writes, never read  │
                     │    by this package)                          │
                     └───────────────┬───────────────────────────────┘
                                      │ writes
                                      ▼
        cycles / strikes / market_structure_snapshots (SQLite)
        data/history/<symbol>/3m.csv|.parquet (candle archive)
                                      │ reads only
                                      ▼
┌──────────────────────────── agents/trading_intelligence/ ─────────────────────────────┐
│                                                                                          │
│  data_access.py  ── thin SQLite/pandas readers, one per shape (latest_cycle,            │
│                      recent_cycles, recent_strike_history, load_candles, ...)           │
│         │                                                                                │
│         ▼                                                                                │
│  market_data.get_snapshot(symbol) ──► MarketSnapshot (ONE read per cycle,                │
│         │                              fills missing Greeks via greeks.py)               │
│         │                                                                                 │
│         ├──────────────► institutional_intelligence.analyze(symbol, snapshot=snapshot)   │
│         │                    (Findings: build-up, OI walls, max pain, gamma trap,        │
│         │                     liquidity sweep, fake breakout, institutional flow)         │
│         │                              │                                                  │
│         ├──────────────► strike_intelligence.build_table(symbol, snapshot.strikes, ...)  │
│         │                    (one StrikeIntelligence row per strike --                    │
│         │                     see "Strike Intelligence fields" below)                     │
│         │                              │                                                  │
│         └──────────────► ai_trading_engine.evaluate(symbol, snapshot=snapshot,           │
│                              findings=<from institutional_intelligence, NOT refetched>)  │
│                                        │                                                  │
│                                        ▼                                                  │
│                              Recommendation (BUY CE/BUY PE/HOLD/NO_TRADE +                │
│                              full Priority-2 field set -- see below)                      │
│                                        │                                                  │
│                    ┌───────────────────┼────────────────────┐                            │
│                    ▼                   ▼                    ▼                            │
│           paper_trading.py      ti_store.py            api.py                            │
│           (enter/close/journal) (ti_paper_trades,       (get_symbol_overview,             │
│                                   ti_signal_log)          get_overview -- the ONE seam     │
│                                                            a Flask route touches)          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                     /admin/trading-intelligence (templates/trading_intelligence.html)
                     /api/trading-intelligence/overview
```

**One snapshot, one institutional-intelligence sweep, per symbol per call** — the single most important data-flow property this review pass fixed (see below). `api.get_symbol_overview()` calls `market_data.get_snapshot()` exactly once and `institutional_intelligence.analyze()` exactly once per symbol, threading both through to `strike_intelligence.build_table()` and `ai_trading_engine.evaluate()` rather than letting each module independently re-fetch the same cycle. Verified by a dedicated regression test (`test_api.py::test_snapshot_and_analysis_are_each_computed_exactly_once`) that counts real call sites via monkeypatching, not just by reading the code.

## Module summaries

**1. Market Data Engine** (`market_data.py` + `data_access.py`) — aggregates OI, OI change, PCR (+ change), IV, Greeks, VWAP, and volume for one symbol from already-stored `cycles`/`strikes`/candle data into one `MarketSnapshot`. "Historical storage" was not a new requirement to build — it already exists (`cycles`/`strikes`/`data/history/*`); this module's job is reading it, honestly reporting `available=False` when a symbol has never had a cycle logged rather than raising. `fill_missing_greeks()` (made public this review pass) is now the ONE shared implementation `strike_intelligence.py` also calls, rather than a second copy.

**2. Institutional Intelligence** (`institutional_intelligence.py`) — the full requested detection list, described above. Every finding carries real evidence (OI change, gamma value, distance to spot, etc.), never a bare label. `analyze()` now accepts an optional pre-fetched `snapshot` (review pass) so a caller that already has one skips a second `cycles`/`strikes` read entirely.

**2b. Strike-level AI Intelligence** (`strike_intelligence.py`) — added per the explicit follow-up request ("BATI pratyek strike sathi... OI Wall / Support-Resistance / PCR / Build-up Type / Max Pain / Expected Move / AI Buy-Sell Probability / CE-PE Strength dakhavel", matching the existing Option Chain screen), then substantially extended in the review pass (Priority 3). Every `StrikeIntelligence` row now carries:

| Field | What it is | Source |
|---|---|---|
| `is_oi_wall_support` / `is_oi_wall_resistance` | Top-3 OI wall membership | `oi_engine.oi_walls` |
| `oi_wall_score` | This strike's `(ce_oi+pe_oi)` as % of the whole chain's OI | new, transparent arithmetic |
| `is_max_pain` / `max_pain_distance` | Max pain flag + distance in points | `oi_engine.calc_max_pain` |
| `ce_buildup_type` / `pe_buildup_type` | Long/Short Buildup, Unwinding, Covering, Neutral | `oi_engine.classify_buildup` (already live) |
| `net_lean` / `ai_buy_probability_pct` / `ai_sell_probability_pct` | OI-positioning read, clamped 10-90% | `oi_engine.net_oi_buildup_lean` |
| `support_strength` / `resistance_strength` | 0-100, OI share scaled by build-up conviction (renamed from `pe_strength`/`ce_strength` — same computation, clearer name) | new, weights documented in `_strength()`'s own docstring |
| `ce_gamma_exposure` / `pe_gamma_exposure` / `ce_delta_exposure` / `pe_delta_exposure` | `OI × Greek` | `greeks.black_scholes_greeks` via `market_data.fill_missing_greeks` |
| `ce_prob_itm` / `pe_prob_itm` | Real risk-neutral N(d2)/N(-d2) Black-Scholes probability, distinct from delta | `greeks.black_scholes_greeks`'s new `prob_itm` key (see below) |
| `ce_iv_rank` / `pe_iv_rank` | 0-100 percentile of this strike's CE/PE IV within its OWN recent history; `None` under 3 readings | `data_access.recent_strike_history` |
| `ce_premium_momentum` / `pe_premium_momentum` | % vs short-term average of recent premium readings | `sr_probability_engine.compute_premium_momentum` |
| `expected_move_pts` | 1-SD expected move by expiry: `spot × IV × √T` | new, textbook formula, public as `strike_intelligence.expected_move()` (also reused by the AI Trading Engine) |
| `ai_strike_score` | 0-100 composite: OI concentration (30%) + build-up conviction (25%) + gamma exposure (20%) + ITM contestedness (25%) | new, weights documented in `_ai_strike_score()`'s own docstring |

Expected Move / AI Buy-Sell Probability / Probability of ITM are three deliberately DIFFERENT numbers (positioning lean vs. risk-neutral pricing vs. this module's own volatility-implied move), documented explicitly in the module docstring to prevent conflation.

**3. AI Trading Engine** (`ai_trading_engine.py`) — BUY CE / BUY PE / HOLD / NO TRADE. HOLD only exists once a position is open (checked before ever generating a fresh signal); Probability is a real historical win-rate calibration from this engine's OWN closed paper trades, bucketed by confidence, honestly `None` with a stated reason until 5+ trades exist in a bucket (never fabricated); Risk Score (0–100, documented as higher = riskier) reuses `agents.risk_manager.risk_engine.position_sizing_check`. Extended this review pass (Priority 2) so every `Recommendation` — on every path, including NO_TRADE and HOLD — carries:

`market_bias`, `confidence`, `probability` (+ `probability_note`), `risk_score`, `entry_price`, `sl_price`, `target_price` (T1) + `targets` (T1/T2/T3, strictly increasing, anchored so `targets[0]` always equals `target_price`), `expected_move_pts`, `time_horizon` (a real days-to-expiry label, never a vague guess), and four structured reasoning strings: `institutional_reasoning`, `oi_reasoning`, `greeks_reasoning`, `price_action_reasoning`.

T2/T3 extend `generate_signal()`'s own single-target projection formula (never a different methodology) onto the 2nd/3rd heaviest OI walls; a further-ranked wall that would project a NEARER price than an earlier target is dropped rather than reordered ahead of T1 (see "Real bugs found and fixed" below for why this matters).

**4. Multi Timeframe Engine** (`multi_timeframe.py`) — a real, important finding, stated plainly rather than worked around: **this repository has only ever archived 3-minute candles.** 15m/30m/1H/Daily are real local pandas resamples of the 3m archive (clean multiples: 5/10/20/full-day). 1m is honestly reported unavailable (finer than the archived data — not recoverable). 5m is *also* honestly reported unavailable, for a less obvious reason: 5 is not a clean multiple of 3, so resampling would silently misrepresent real bar boundaries rather than just being "approximate." Re-reviewed this pass (Priority 4) and found already airtight from the original build — `UNAVAILABLE_TIMEFRAMES` states both reasons in code, `get_timeframe()`/`synchronize()` both return an honest `available: False` + `reason` rather than raising or fabricating bars, and this is directly covered by `test_multi_timeframe.py`. No production code change was needed here; the finding itself IS the deliverable.

**5. Paper Trading** (`paper_trading.py` + `ti_store.py`) — a new `ti_paper_trades` table (not a reuse of `app.py`'s own `paper_trades`/`scalp_paper_trades`/`v3_paper_trades` — this is a distinct engine with its own lifecycle, the same way each of those already has its own dedicated table). Every write is a pure SQLite INSERT/UPDATE with the price supplied by the caller — the exact pattern `app.py`'s own `db_open_paper_trade()` already establishes. `enter_from_recommendation()` now passes the real `Recommendation.strike` through to `ti_store.open_trade()` (see "Real bugs found and fixed" — this was a genuine bug this review pass caught and fixed).

**6. Dashboard** (`api.py` + `templates/trading_intelligence.html` + `/admin/trading-intelligence`, `/api/trading-intelligence/overview` in `app.py`) — live option chain, OI analytics, Greeks, AI signals, risk, confidence, paper P&L, agent health, all in one admin-gated page, folded into the same Flask app as every other admin dashboard rather than a separate service.

**7. Safety** — see "the central safety decision" above.

## Review pass: what changed and why

Requested explicitly before merge approval: remove duplicated logic, enrich every recommendation and per-strike output to institutional-grade completeness, formalize the 1m/5m feasibility finding, and run a dedicated validation suite. Concretely:

- **Priority 1 (deduplication)** — `api.get_symbol_overview()` was calling `market_data.get_snapshot()` up to 2x and running a full `institutional_intelligence.analyze()` up to 2x per symbol per call (once directly, once again inside `ai_trading_engine.evaluate()`'s own reasoning-enrichment step). Fixed by threading an optional pre-fetched `snapshot`/`findings` through `institutional_intelligence.analyze()` and `ai_trading_engine.evaluate()`. Regression-tested (see Architecture section above).
- **Priority 2 (recommendation enrichment)** — see Module 3 above for the full new field list.
- **Priority 3 (strike intelligence enrichment)** — see Module 2b above for the full new field list.
- **Priority 4 (1m/5m feasibility)** — re-verified as already correctly and honestly handled from the original build; no code change needed.
- **Priority 5 (validation)** — a new `test_validation.py` covering Replay, Stress, Performance, Memory, and Trading validation end-to-end (see "Validation summary" below). This is where the two real bugs below were actually caught.
- **Priority 6 (this document)**.

## Real bugs found and fixed

**Original build:**

- `data_access._row_to_strike_row()`'s first version passed SQLite `NULL` straight through as Python `None` for any Greeks column never written that cycle (e.g. before the IV/Greeks migration ran, or a strike Angel One's feed didn't populate). `oi_engine.StrikeRow`'s own dataclass defaults every numeric field to `0.0`, never `None` — every consumer of a `StrikeRow` in `oi_engine.py` assumes that. Found immediately by this milestone's own first integration test, fixed by coercing `None` to each field's own dataclass default explicitly.
- `pandas` 3.0 rejects the frequency strings `"15m"`/`"1H"` outright (`ValueError: 'm' is no longer supported for offsets`) — `multi_timeframe.py`'s resample rules use the pandas-3.x-correct `"15min"`/`"30min"`/`"1h"` instead, caught before this module ever shipped by testing the resample against the real archive.

**Review pass (this pass):**

- **Duplicate snapshot/analysis fetches** (Priority 1) — see above.
- **T1/`targets[0]` could silently disagree** — `_multi_targets()`'s original implementation put all three candidate targets into a set, sorted the set, and returned the result. Since `oi_walls()` ranks by OI weight (not by price distance), a 2nd/3rd-ranked wall could project a price CLOSER than T1, and the sort would then put that closer price first — meaning `Recommendation.targets[0]` (what a "T1/T2/T3" UI would show as the nearest target) could differ from `Recommendation.target_price` (the field `_check_open_trade_exit()` actually watches to close a paper trade). Caught while writing a new test asserting `targets[0] == target_price` (a property the module's own docstring already implicitly promised). Fixed by anchoring `targets[0]` to `target_price` unconditionally and only appending further walls' projections when they're strictly greater than the previous target, rather than reordering by price.
- **Paper trades could never auto-close (the most consequential finding of this review pass)** — `paper_trading.enter_from_recommendation()` called `ti_store.open_trade(..., strike=None, ...)` because `Recommendation` never carried the strike a signal was generated from. `ai_trading_engine._check_open_trade_exit()` matches an open trade against the current cycle's snapshot via `row.strike == trade["strike"]`; with `trade["strike"]` always `None`, that match could never succeed, so a trade opened through the normal `evaluate()` → `enter_from_recommendation()` path would sit open forever regardless of what the market did — every future `evaluate()` call would just report `HOLD` indefinitely. Caught by `test_validation.py`'s end-to-end trading-lifecycle test (evaluate → enter → simulate a target-hit price move → evaluate again → expect an auto-close), which is exactly the path a live scheduler cycle would drive and exactly why that test exists as an end-to-end scenario rather than three isolated unit tests. Fixed by adding a `strike` field to `Recommendation`, populating it on every return path in `evaluate()` (the open BUY, the HOLD, and the just-closed NO_TRADE), and passing it through in `enter_from_recommendation()`.

## Real options-pricing addition: Probability of ITM

`greeks.black_scholes_greeks()` (repo root — the SAME function `oi_engine.generate_signal()` and this milestone's Market Data Engine already call) was extended additively with a `prob_itm` key: the real risk-neutral `N(d2)` (calls) / `N(-d2)` (puts) probability of finishing in-the-money, computed from the exact same `d1`/`d2` already derived for delta/gamma — not the common "delta ≈ probability ITM" trader shortcut, which the code explicitly does not rely on. Existing callers are unaffected (`delta`/`gamma`/`valid` keys unchanged); `test_engine.py`'s full 33-test suite for `oi_engine.py`'s own consumers of this function was re-run and shows zero regressions.

## Database schema

```sql
ti_paper_trades   -- this engine's own paper trades (NEW table, not shared with app.py's other engines)
ti_signal_log     -- every AI Trading Engine recommendation, including NO_TRADE/HOLD, for explainability
```

Both in `oi_history.db`, indexed from the start, `PRAGMA busy_timeout=5000` on every connection — matching every prior milestone's own store convention. No new tables were added to `cycles`/`strikes`/`market_structure_snapshots` — those stay entirely owned by `app.py`. Unchanged by the review pass.

## Validation summary (Priority 5)

A dedicated `test_validation.py` (7 tests) was added, covering exactly the five categories requested, each with real (not rubber-stamp) assertions against realistic-shaped data:

| Category | What was run | Result |
|---|---|---|
| **Replay** | A 20-cycle sequence with an oscillating underlying and shifting PCR, re-evaluating the full `get_symbol_overview()` pipeline every cycle | Never raised; every cycle's recommendation and strike table stayed internally consistent (entry/SL/target always co-present, `ai_strike_score` always bounded) |
| **Stress** | A 25-strike chain (`strikes_each_side=12`, wider than any prior unit test) + a 60-reading-deep per-strike history | `oi_wall_score` correctly summed to ~100% across the full chain; exactly one `is_max_pain` strike; IV Rank / Premium Momentum stayed bounded over the long history |
| **Performance** | Wall-clock budget on `get_symbol_overview()` (5 calls, 9-strike chain) and `strike_intelligence.build_table()` (25-strike chain) | Both well under their (generous, SQLite-on-tmpfs) budgets — no N²-shaped regression detected |
| **Memory** | Open file-descriptor count via `/proc/self/fd` before/after 50 repeated `get_symbol_overview()` calls | No growth beyond noise (≤2 fds) — confirms `data_access.py`'s "open and close a connection per call" contract holds under load, not just by reading the source |
| **Trading** | A full lifecycle driven exactly the way a live scheduler cycle would: `evaluate()` → `enter_from_recommendation()` → simulate a target-hit price move → `evaluate()` again → `performance_stats()` | **Caught the strike=None bug above on first run** (`HOLD` instead of the expected auto-close `NO_TRADE`); passes now, with the trade correctly auto-closing and `performance_stats()` reflecting one win |

Full `test_agents/trading_intelligence/` suite after the review pass: **109 tests, all passing** (up from the original build's 83 — 26 new tests: 14 new/expanded Strike Intelligence cases, 4 new AI Trading Engine cases covering the Priority-2 fields and the T1/targets anchoring, a dedup regression test, a paper-trade strike regression test, and the 7-test validation suite).

Full repository suite after the review pass: **1,209 passed, 1 xfailed**, zero regressions (re-run in full after every production-code change in this pass, not just the trading_intelligence subdirectory).

## Known, documented limitations

- **No live `oi_history.db` exists in this dev/CI environment** (consistent with every prior milestone's own note) — every test here builds its own realistic synthetic schema. Everything in this milestone reads the exact same tables `app.py`'s live loop writes in production; nothing about the code path differs between "synthetic test data" and "real live data."
- **1m and 5m candle timeframes are not available**, honestly, for the reasons in Module 4's own section above — closing this gap would require a genuine historical fetch from Angel One via `history_engine.py`, out of scope for a module that reads what's already archived (and would need the shared live session anyway).
- **Gamma Trap and Institutional Buying/Selling are new, advisory-only heuristics**, not validated against real historical outcomes the way `oi_engine.py`'s core signal logic has been through years of live use — flagged as such in their own docstrings, same as `oi_engine.compute_new_trend_meter`'s own honesty precedent.
- **AI Buy/Sell Probability (per-strike, Module 2b), Probability (AI Trading Engine, Module 3), and Probability of ITM (per-strike, Module 2b) are three deliberately different numbers**, computed from different evidence (OI positioning lean, historical trade calibration, and risk-neutral options pricing respectively) — documented explicitly in `strike_intelligence.py`'s own module docstring to prevent conflation.
- **`ai_strike_score`, `support_strength`/`resistance_strength`, and the risk score are transparent, hand-weighted arithmetic, not fitted or backtested models.** The weights are documented in-place in each function's own docstring specifically so a future reviewer can see and question them, not because they've been empirically validated to be optimal.
- **Probability calibration starts genuinely empty.** A brand-new deployment's `ai_trading_engine.evaluate()` will report `probability=None` with an honest "insufficient history" note for every confidence bucket until 5+ real closed paper trades land in that bucket — by design, never a fabricated number standing in for real history.
- **Nothing in this milestone is wired into the Milestone 9 runtime scheduler yet** — `ai_trading_engine.evaluate()`/`paper_trading.enter_from_recommendation()` are real, callable, tested functions, but no `agents/runtime/agent_runtime.py` cycle invokes them automatically. Wiring a seventh scheduled cycle in is a natural, small follow-up, deliberately left out of this milestone's own scope ("focuses ONLY on trading intelligence, market analysis and paper trading").
- **`templates/trading_intelligence.html` was not extended this review pass** to surface every new field (Market Bias, T2/T3, structured reasoning sections, the new Strike Intelligence columns) — the review's explicit scope was the engine and its output correctness, not the UI. `api.get_symbol_overview()`'s JSON payload already carries every new field (verified JSON-serializable by `test_api.py`); wiring the template to display them is a small, isolated follow-up.

## Future improvements (not built, deliberately out of scope)

- ~~Wire the AI Trading Engine into the Milestone 9 scheduler as a genuine autonomous cycle (with the Human Approval Engine gating any eventual move beyond paper trading).~~ **Done** -- `api.run_scheduled_cycle()` (the function the runtime scheduler actually calls) already invokes `ai_trading_engine.evaluate()` and `paper_trading.enter_from_recommendation()` every cycle.
- ~~Extend `templates/trading_intelligence.html` to render the new Strike Intelligence and Recommendation fields.~~ **Done** -- the template now renders the Strike Intelligence fields.
- ~~Once enough real paper-trade history accumulates, consider backtesting Gamma Trap and Institutional Buying/Selling against it the same way `oi_engine.py`'s core signals have been, and either graduate them out of "advisory-only" or document why they don't hold up.~~ **Done for Institutional Buying/Selling** (real paper-trade history still has zero examples, so this used a real historical `cycles`/`strikes` archive replay instead -- see `INSTITUTIONAL_FLOW_BACKTEST_REPORT.md`: no symbol's win rate is statistically distinguishable from 50%, so it stays advisory-only). Gamma Trap remains untested -- no historically-reconstructable `expiry_date` exists anywhere in this repo (see that report's own Scope section for why).
- ~~If a genuine historical 1m/5m fetch is ever run via `history_engine.py`, `multi_timeframe.py`'s `UNAVAILABLE_TIMEFRAMES` entries can be removed and real (not resampled) 1m/5m support added — the module already documents exactly what would need to change.~~ **Done** -- `UNAVAILABLE_TIMEFRAMES` no longer exists in `multi_timeframe.py`; real 1m/5m support has been added.

- Dual-probability calibration (`dual_probability_*.py`, PR #27): shadow-only infrastructure, real validation run twice against genuine historical/live-signal data -- see `DUAL_PROBABILITY_CALIBRATION_REPORT.md` for the full findings. Target-probability model has never validated at any tested horizon; stop-safety model only validates in 3 of 8 symbol/direction cases with one confirmed overfitting failure. `dual_probability_store.py` deliberately not built and no shadow node wired into `signal_graph.py` until one of that report's two unblocking conditions is met -- this is an evidence gap, not a code task.
- Momentum-confirmation sub-score (`TI_ENABLE_MOMENTUM_CONFIRMATION`, PR #29/#31): deployed OFF, never validated before now -- see `MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md`. Real backtest against 5+ weeks of historical data across every watched symbol found no evidence supporting turning it on: the one symbol with a real, floor-clearing sample (BANKNIFTY) got measurably worse (win rate 25% vs. a raw 31.6% OFF, net points down 363.35), and the other floor-clearing symbols (CRUDEOILM/GOLDM/SILVERM) are already deeply unprofitable regardless of this flag. Stays OFF -- this would need a different feature/threshold design or more accumulated history before it's worth re-testing, not a code change.
- `oi_engine.generate_signal()`'s entry/SL/target formula itself was, until now, never unit-tested or backtested on its own terms -- see `ENTRY_SL_TARGET_BACKTEST_REPORT.md` and the new `test_oi_engine_signal_math.py`. The arithmetic is structurally correct (0 invariant violations across 2,405 real backtested trades). Its real-world performance at today's parameters is not: aggregate profit factor 0.56, net -19,389.90 points over the full ~6-week archive, and only 1 of 6 symbols with a trustworthy resolved-trade sample (NIFTY) is even close to breakeven. No parameter change was made -- retuning `target_delta_approx`/`sl_percent`/`min_target_percent`/`MAX_HOLD_MINUTES` needs its own before/after backtest, not a guess.
- That retune was then actually attempted -- see `SL_TARGET_RETUNE_REPORT.md`. An 18-combination grid search (`sl_percent` x `min_target_percent` x `MAX_HOLD_MINUTES`) found a candidate that looked meaningfully better on training data (profit factor 0.85 -> 0.92), but a proper train/test split caught it as overfitting: the same candidate collapsed to profit factor 0.23 on held-out data, 4x worse than the current live defaults. No parameter change was made -- the current defaults are, on this evidence, the best of everything tested. The report's own read: with SL/target/hold-time all clustered in a narrow 0.57-0.92 profit-factor band regardless of how they're varied, the real bottleneck is more likely entry/bias selection (which trades get taken) than exit sizing (how they're managed once taken) -- a separate, larger investigation, not started.
- That entry/bias-selection investigation was then run -- see `ENTRY_BIAS_SELECTION_REPORT.md`. Two independent checks against the same archive: confidence-score calibration (Pearson correlation between `confidence` and actual trade outcome across 2,405 trades: -0.0115, indistinguishable from zero) and the raw directional accuracy of `detect_bias()`'s own CE/PE call, stripped of every other mechanic (does the underlying actually move the signaled direction within 15 minutes? 1,150/2,303 = 49.9%, a coin flip). Neither is a coding bug -- both run exactly as documented -- but together they explain why no SL/target/hold-time combination in the retune report crossed breakeven: exit-parameter tuning cannot create an edge that doesn't exist at entry. No code changed -- this is a diagnosis, not a fix; redesigning the bias/confidence logic to find a real edge is a materially larger undertaking, not started.

Every other item in this section is resolved.

## Test summary

109 tests across `test_agents/trading_intelligence/` (13 files: `test_data_access.py`, `test_market_data.py`, `test_institutional_intelligence.py`, `test_strike_intelligence.py`, `test_ai_trading_engine.py`, `test_multi_timeframe.py`, `test_ti_store.py`, `test_paper_trading.py`, `test_api.py`, `test_safety.py`, `test_validation.py`, plus `conftest.py`'s shared fixtures), including a real 9-to-25-strike synthetic option chain fixture (`insert_realistic_chain`), real OHLC-aggregation verification against actual archived NIFTY candles, a full HOLD → target-hit → auto-close → journal round trip, and the five-category validation suite above.

`agents/trading_intelligence/`: 10 files, 1,716 lines. `test_agents/trading_intelligence/`: 13 files, 1,246 lines.

Full repository suite: **1,209 passed, 1 xfailed** (up from 1,183 after the original build, 1,100 pre-Milestone-10), zero regressions.
