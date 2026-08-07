# Module 11.3 Report — Trade Quality Scoring & Multi-Dimensional Calibration

## Objective

Give this engine a genuinely new, honest feedback signal, distinct from win/loss: for every CLOSED paper trade, did its own reasoning (regime alignment from Module 11.1, timeframe confirmation from Module 11.2, institutional-finding backing) actually match what happened to it? A well-supported trade that won is high quality. A well-supported trade that lost is a fair bet that didn't pay off — meaningfully different from a badly-reasoned loss. A trade with no real support that won anyway is a lucky outcome, not evidence the reasoning was sound.

Separately, extend `ai_trading_engine.calibration_report()` — previously confidence-only — to accept an optional second bucketing dimension, following the exact multi-dimensional pattern `backtest.score_calibration_report()` already validated for the S/R engine.

## Design decisions

1. **Context must be captured live, at entry — never recomputed after the fact.** `regime_profile.classify()` and `timeframe_confirmation.check()` both read the *latest* snapshot in their respective tables; neither supports "as of a past timestamp." Computing them after a trade closes would silently score the trade against *today's* market state, not the state that existed when it was opened — exactly the kind of retroactive fabrication this project's honesty discipline (the 1m/5m precedent) forbids. So `paper_trading.enter_from_recommendation()` — the one place a trade is actually opened — now calls `regime_profile.classify()`, `timeframe_confirmation.check()`, and a new `trade_quality.institutional_backing()` helper once, at open time, and persists the results on the `ti_paper_trades` row itself.

2. **This is the first M11 module to wire M11.1/M11.2 into the live trade path** — but only as far as strictly needed. `enter_from_recommendation()` only needs `recommendation.symbol` and `recommendation.direction`, both of which `Recommendation` already carries; `ai_trading_engine.Recommendation` itself gains no new field and `evaluate()` is untouched. Wiring regime/timeframe data all the way into `Recommendation`'s own reasoning text remains deferred, unchanged from M11.1/M11.2's own stated scope.

3. **Schema change: four new nullable columns on `ti_paper_trades`**, not a parallel table (the plan left this decision open). Added via the same self-migrating `PRAGMA table_info()` + `ALTER TABLE ... ADD COLUMN` pattern `app.py` and `agents/sys_admin/sysadmin_store.py` already use — safe to run against a live production database that predates this column set. `open_trade()`'s four new kwargs all default to `None`; every existing caller and test is unaffected.

4. **Trade Quality Score formula** (`trade_quality.score()`), matching `strike_intelligence._ai_strike_score()`'s own "named constants, transparent arithmetic, no fitted model" convention:
   - `setup_strength` (0–100): the average of whichever of the three components were actually captured (`REGIME_TREND_SCORE` mapping: TRENDING=100, TRANSITIONING=60, RANGING=40; institutional backing: 100/0; timeframe alignment score passed through as-is). A missing component is *excluded* from the average, never fabricated as a 50.
   - `outcome_alignment` (0–100, binary): 100 if the setup's own confidence direction (`setup_strength >= 50`) matched the real outcome (won/lost), 0 if it was a "surprise" either way. Deliberately a single transparent formula rather than an invented multi-bucket table — avoids the "complexity creep" risk the plan itself flagged.
   - Final score = `0.5 * setup_strength + 0.5 * outcome_alignment`.
   - A trade with **zero** captured components (opened before this instrumentation, or every reading was genuinely unavailable that cycle) returns `available=False, score=None` with a stated reason — never a fabricated mid-point.

5. **`calibration_report(dimension=None)`**: with `dimension` omitted, the return value is byte-identical to before this module (verified by a dedicated regression test: `calibration_report() == calibration_report(dimension=None)`, and both still a plain `list`). Passing `dimension="regime"|"timeframe_alignment"|"quality_tier"` returns `{"by_confidence": <the same list>, f"by_{dimension}": {...}}` — a second, independent breakdown, not a cross-bucketed combination, matching `score_calibration_report()`'s own `by_tier`/`by_regime` shape (one pass over closed trades, one bucket dict per key, `CALIBRATION_MIN_SAMPLE`-gated honesty per bucket).

## Files changed

- **New**: `agents/trading_intelligence/trade_quality.py` — `TradeQualityScore` dataclass, `score()`, `institutional_backing()`, `quality_tier()`.
- **New**: `test_agents/trading_intelligence/test_trade_quality.py` (27 tests).
- **Modified**: `agents/trading_intelligence/ti_store.py` — four new nullable `ti_paper_trades` columns (self-migrating), `open_trade()` gains four optional kwargs.
- **Modified**: `agents/trading_intelligence/paper_trading.py` — `enter_from_recommendation()` now captures entry-time context and accepts optional `snapshot`/`findings` for the same dedup discipline `evaluate()` already uses.
- **Modified**: `agents/trading_intelligence/api.py` — `run_scheduled_cycle()` passes its already-fetched `snapshot`/`findings` through to `enter_from_recommendation()` (one line, avoids a second institutional-intelligence sweep per cycle).
- **Modified**: `agents/trading_intelligence/ai_trading_engine.py` — `calibration_report()` gains the optional `dimension` parameter, `_calibration_dimension_key()`/`_bucket_by_dimension()` helpers.
- **Modified**: `test_agents/trading_intelligence/test_ti_store.py`, `test_paper_trading.py`, `test_ai_trading_engine.py` — 13 new tests covering the above.

No existing file's prior behavior changed for a caller that doesn't opt in — every touch-point above is either a brand-new file or an additive, default-preserving parameter/column.

## Tests executed

- Module suite (`test_trade_quality.py`): **27/27 passed**.
- Full `test_agents/trading_intelligence/` suite: **194/194 passed** (up from 154).
- Full repository suite: **1,296 passed, 1 xfailed** (up from 1,256), **zero regressions**.

Coverage highlights:
- `TestScore` / `TestRegimeComponent` / `TestInstitutionalComponent`: every branch of the scoring formula, including the "no context at all" honest-degradation path and the zero-points-is-a-loss edge case.
- `TestScoreLifecycle`: the plan's own success criterion — opens and closes 10 real trades (half with captured context, half without) and confirms `score()` runs for all 10 without raising, correctly distinguishing available from unavailable.
- `TestInstitutionalBacking`: agreeing/wrong-side/wrong-strike/non-institutional findings, plus the honest `None`-vs-`False` distinction for genuinely unavailable data vs. "checked and found nothing."
- `TestEntryTimeReasoningContext` (`test_ti_store.py`): column defaults, roundtrip through open→close, SQLite's 0/1 boolean storage.
- `TestEntryTimeReasoningContextCapture` (`test_paper_trading.py`): honest degradation with no underlying data, real regime capture against a real chain + market-structure row, and the prefetched-`snapshot`/`findings` dedup path.
- `TestCalibrationReportDimension` (`test_ai_trading_engine.py`): the critical `dimension=None` byte-identical regression guard, invalid-dimension rejection, `CALIBRATION_MIN_SAMPLE` honesty gate per bucket, `UNKNOWN` bucketing for missing context, and a mixed-scoreability stress test for the `quality_tier` dimension.

## Performance impact

`enter_from_recommendation()` now does real work at trade-open time: one `regime_profile.classify()` call (reads `market_structure_snapshots` + recent strike history), one `timeframe_confirmation.check()` call (reads the already-derived 15m/30m/1h candles), and one `institutional_backing()` check (reuses the `findings` list `run_scheduled_cycle()` already computed for `evaluate()` when passed through — no second institutional-intelligence sweep on the normal scheduled-cycle path). This only runs when a trade is *actually opened* (a small fraction of cycles — most cycles are NO_TRADE/HOLD), not on every cycle. `calibration_report(dimension=...)` does one extra pass over `list_closed_trades()` (already bounded at `limit=10_000`, same as the existing confidence pass) only when a caller explicitly opts into a dimension; the default call path is unchanged.

## Risks

- **`REGIME_TREND_SCORE` and the 50/50 `setup_strength`/`outcome_alignment` weighting are documented, transparent design choices, not fitted or backtested values** — same honesty as Module 11.2's own alignment thresholds. If real trade history later shows they need adjustment, that's a constant change, not a rewrite.
- **Historical trades opened before this commit have no entry-time context** and will always score `available=False` — by design (no retroactive fabrication), but it means the Trade Quality Score's real-world sample starts at zero and only grows from new trades going forward, the same "starts empty, never fabricated" honesty already established for the Probability calibration framework itself.
- **`calibration_report()`'s `dimension` parameter is new surface area**, but it's strictly additive: the default (`None`) path is covered by a dedicated byte-identical regression test, so no existing caller (including the dashboard, if it calls this today) can be affected without explicitly opting in.
- `trade_quality.py` is not yet surfaced on any dashboard view — this module is the scoring engine and its wiring into the live trade-open path, not a UI change (none was requested).

## Commit hash

`b9e5c27` on `worktree-m11-intelligence-depth`.

---

Waiting for approval before starting Module 11.4 (Explainable AI Reasoning).
