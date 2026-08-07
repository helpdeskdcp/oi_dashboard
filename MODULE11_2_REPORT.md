# Module 11.2 Report — Multi-Timeframe Probability Engine

## Objective

Milestone 10's `multi_timeframe.py` is purely a data-availability module — `get_timeframe()`/`synchronize()` resample the native 3m archive into 15m/30m/1h/daily, but nothing anywhere in the repository checks whether a signal actually *agrees* with what a higher timeframe is doing. This module adds exactly that: for a given `(symbol, direction)`, it reads each of `multi_timeframe.DERIVABLE_TIMEFRAMES` (15m/30m/1h — 1m/5m correctly stay untouched, inheriting `multi_timeframe.py`'s own honest unavailability) and asks whether that timeframe's own recent close-over-close movement agrees with the direction (CE = bullish, PE = bearish). The result is a plain ratio — agreeing timeframes over available timeframes — never a fitted model, never a fabricated probability.

## Files changed

- **New**: `agents/trading_intelligence/timeframe_confirmation.py` — `TimeframeReading`/`TimeframeAlignment` dataclasses, `_bar_trend()`, `check()`.
- **New**: `test_agents/trading_intelligence/test_timeframe_confirmation.py`

No existing file was modified. `multi_timeframe.py` itself is untouched — this module only reads its already-public `DERIVABLE_TIMEFRAMES` dict and `get_timeframe()` function.

## Tests executed

14 new tests:
- `TestBarTrend` (6) — direct unit tests of the trend-reading function against synthetic candle DataFrames (too-few-bars, None/empty, UP, DOWN, FLAT, and confirming only the lookback window matters, not older history).
- `TestCheckValidation` (1) — invalid `direction` raises `ValueError`.
- `TestCheckIntegration` (6) — against the **real archived NIFTY candle data**, the same convention `test_multi_timeframe.py`'s own `TestGetTimeframe` suite already established, never synthetic data: reading shape, score bounds, agreement-count bounds, a real cross-check that CE and PE can never both be fully confirmed by the same underlying data, and honest degradation for an unknown symbol.
- `TestStress` (1) — 5 symbols × 2 directions, confirms `check()` never raises, matching the plan's own stress-test success criterion.

Results:
- Module suite: **14/14 passed**.
- `test_agents/trading_intelligence/` full suite: **154/154 passed** (up from 140).
- Full repository suite: **1,256 passed, 1 xfailed** (up from 1,242), **zero regressions**.

## Performance impact

Negligible. `check()` calls `multi_timeframe.get_timeframe()` for 3 timeframes — the exact same already-derived-candle read path a dashboard load already exercises via `api.get_multi_timeframe_summary()`. No new database reads, no new external calls, no new heavy computation (the trend read is a single close-vs-close comparison over a small, fixed-size tail slice of an already-in-memory DataFrame).

## Risks

- **Not yet wired into `ai_trading_engine.Recommendation`** — this is a standalone, independently-tested module, matching Module 11.1's own "implement one module at a time" discipline. Wiring it in as a new optional `Recommendation` field is deferred to a later integration step.
- `TREND_LOOKBACK_BARS = 4` and the `ALIGNMENT_CONFIRMED_PERCENTILE`/`ALIGNMENT_CONTRARY_PERCENTILE` thresholds (66.7%/33.3%, deliberately reused from Module 11.1's own volatility-banding split for consistency) are documented, transparent design choices — not fitted or backtested values. If real usage shows they need tuning, that's a parameter change, not a rewrite.
- `_bar_trend()` uses zero noise threshold (any nonzero close-over-close difference counts as a direction) rather than a magnitude cutoff — a deliberate choice to avoid an arbitrary "how big a move counts" constant, but means a very small drift can register as UP/DOWN rather than FLAT. Documented in the function's own docstring.

## Commit hash

`5f4558d` on `worktree-m11-intelligence-depth`.

---

Waiting for approval before starting Module 11.3 (Trade Quality Scoring & Multi-Dimensional Calibration).
