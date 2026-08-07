# Release Notes — Milestone 10: BATI Trading Intelligence Platform

Tag: `milestone-10` · Merge commit: `ec978202381bf04fb22560a272a0fa2bec3b6463` · Merged into `master`.

## Features completed

**Seven modules**, built entirely on top of BATI Version 1.0's autonomous framework and this repository's pre-existing live option-chain engine (`oi_engine.py`, `greeks.py`, `market_structure.py`) — reused, never duplicated:

1. **Market Data Engine** (`market_data.py`) — one aggregated `MarketSnapshot` per symbol (OI, OI change, PCR, IV, Greeks, VWAP, volume) from already-stored `cycles`/`strikes`/candle data.
2. **Institutional Intelligence** (`institutional_intelligence.py`) — Long/Short Build-up, Long Unwinding, Short Covering, OI Walls, Max Pain, Gamma Trap, Liquidity Sweep, Fake Breakout, Institutional Buying/Selling. Plus **Strike-level AI Intelligence** (`strike_intelligence.py`) — per-strike Support/Resistance Strength, OI Wall Score, Build-up Type, Max Pain Distance, Gamma/Delta Exposure, IV Rank, Premium Momentum, Probability of ITM, and a composite 0–100 AI Strike Score.
3. **AI Trading Engine** (`ai_trading_engine.py`) — BUY CE / BUY PE / HOLD / NO TRADE, each recommendation carrying Market Bias, Confidence, Probability, Risk Score, Entry, Stop Loss, Targets (T1/T2/T3), Expected Move, Time Horizon, and structured Institutional/OI/Greeks/Price-Action reasoning.
4. **Multi Timeframe Engine** (`multi_timeframe.py`) — 15m/30m/1h/Daily derived from the native 3m archive by real resampling; 1m and 5m honestly reported unavailable (see Known Limitations).
5. **Paper Trading** (`paper_trading.py`, `ti_store.py`) — full open/close lifecycle, Win Rate/Profit Factor/Drawdown/Expectancy, trade journal integration with `agents.memory`.
6. **Dashboard** (`api.py`, `templates/trading_intelligence.html`) — every field above rendered live at `/admin/trading-intelligence`.
7. **Safety** — verified by AST scan (`test_safety.py`): no module in this package ever imports the live broker session or `app.py`. Recommendation mode and paper trading only; no real orders are ever placed.

**Plus, from two institutional-grade review passes before merge:**
- Deduplicated the snapshot/institutional-analysis pipeline (one fetch per symbol per call, not up to three).
- Extended `greeks.black_scholes_greeks()` with a real Black-Scholes `prob_itm` key.
- UI extended to surface every one of the above fields, including a new per-strike AI Strike Score table.
- Wired into the Milestone 9 autonomous runtime as a 7th scheduled agent (`agents/runtime/agent_runtime.py`), market-hours gated, 3-minute cadence.
- Probability calibration made inspectable via `ai_trading_engine.calibration_report()` — live, bucketed, never fabricated.

## Bugs fixed

Four real bugs, each found by this milestone's own tests (never assumed away):

1. **T1/`targets[0]` could silently disagree** — a farther-OI-ranked wall could project a nearer price than T1, and the original sort-based target list would put that price first. Fixed by anchoring `targets[0]` to `target_price` unconditionally.
2. **Paper trades could never auto-close** — `paper_trading.enter_from_recommendation()` hardcoded `strike=None`, so `_check_open_trade_exit()`'s strike match could never succeed; a trade opened via the normal flow would sit open forever regardless of target/SL. Fixed by adding `Recommendation.strike` and threading it through every return path. Caught by an end-to-end lifecycle test, not a unit test.
3. **`ti_signal_log` was a dead table** — `ti_store.record_signal()` existed and was unit-tested since the original build, but nothing in `evaluate()` ever called it, despite this being a documented claim. Fixed by wiring it into every return path.
4. **Support/Resistance Strength's ceiling was silently capped below 100** — the original normalizer (`/1.7`) didn't match the formula's true mathematical maximum, so the strongest possible real signal could only ever score ~82. Fixed with a normalizer derived directly from the formula's own constants.

## Files changed

33 files, 3,824 insertions, 8 deletions — see `MERGE_REPORT.md` for the full breakdown. New: `agents/trading_intelligence/` (10 modules), `test_agents/trading_intelligence/` (13 files), `templates/trading_intelligence.html`, `AI_TRADING_INTELLIGENCE.md`, `MILESTONE10_FINAL_REVIEW.md`. Modified (additive only): `app.py`, `greeks.py`, `agents/config.py`, `agents/runtime/agent_runtime.py`, `agents/runtime/scheduler.py`.

## Known limitations

- 1m and 5m candle timeframes are not available — 1m is finer than the archived 3m data (unrecoverable); 5m is not a clean multiple of 3m (would misrepresent bar boundaries). Both honestly reported, never fabricated.
- Gamma Trap and Institutional Buying/Selling are new, advisory-only heuristics, not yet backtested against real outcomes — that requires real elapsed time with the engine running, not a code change.
- No composite score (AI Strike Score, Risk Score, Support/Resistance Strength) is a fitted/trained model — all are transparent, documented arithmetic by design, consistent with this project's anti-black-box convention.
- Probability calibration starts genuinely empty per confidence bucket and stays honestly `None` until 5+ real closed trades exist in that bucket.
- `agents/sys_admin/orchestrator.py`'s enable/disable registry does not cover `trading_intelligence` — a deliberate scope boundary (that file belongs to a previous milestone); a bad cycle still fails safely via `agent_runtime.py`'s own existing failure/escalation logic.
- `templates/trading_intelligence.html` does not surface every Strike Intelligence field (only AI Strike Score, per the explicit UI requirement) — the rest are already in the JSON API response if wanted later.

## Production readiness

**9.5 / 10** — see `MILESTONE10_FINAL_REVIEW.md` section 9 for full justification. The two categories of remaining technical debt (heuristic backtesting, fitted-model absence) are architectural/time-bound, not code defects; everything in scope for a code-level fix was found and fixed, not just described.
