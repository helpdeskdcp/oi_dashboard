# Module 11.6 Report — Performance Analytics Extension

## Objective

Add Sortino Ratio and an Equity Curve time series to `agents/quant_researcher/metrics.compute_stats()` — the one shared statistics definition every stats consumer in this repository already uses (S/R, V3, Ichimoku, dynamic SR v4, Quant Researcher, and Trading Intelligence paper trading) — so no engine ends up with its own, possibly-drifting copy of these definitions.

## Design decisions

1. **Sortino Ratio formula, matching the existing Sharpe Ratio convention exactly.** `backtest.compute_advanced_trade_stats()`'s own Sharpe Ratio is deliberately a per-trade-points ratio (`mean(points) / stdev(points)`), not calendar-annualized, honestly `None` when standard deviation is zero. Sortino Ratio here follows the identical shape: `mean(points) / downside_deviation`, where `downside_deviation = sqrt(mean(min(p, 0)^2 for p in points))` — the standard target=0 semi-deviation — honestly `None` when there's no downside variance to divide by (every trade a win) or fewer than 2 trades exist. Same honesty gate, same units, same "no fabricated annualization" discipline.

2. **Equity Curve is not a new computation** — `compute_advanced_trade_stats()` already walks `equity += p` per trade internally to derive `max_drawdown`; this module exposes that same running cumulative sum as a list instead of collapsing it into one number.

3. **Verified against a hand-computed reference value**, per the plan's own explicit Module 11.6 success criterion. For the fixed sequence `[10, -5, 20, -15, 5]`: mean = 3.0, downside squares = `[0, 25, 0, 225, 0]`, downside deviation = √50 ≈ 7.071, Sortino = 3.0 / 7.071 ≈ 0.424; equity curve = `[10, 5, 25, 10, 15]`. Both values were computed independently by hand (and cross-checked in a throwaway script) before being hard-coded into the test, not derived from the implementation itself.

4. **Purely additive — two new dict keys, nothing else changes.** `compute_stats()` still starts from `dict(data_access.compute_advanced_trade_stats(trades))` unchanged; `sortino_ratio` and `equity_curve` are computed independently from the raw `trades` list (the same list `compute_advanced_trade_stats()` itself receives) and added on top. A dedicated regression test (`test_pre_existing_fields_are_untouched_by_this_module`) asserts every pre-existing field's *value* is unchanged, satisfying "maintain byte-identical default behavior" for everything that isn't the new feature itself.

5. **`paper_trading.performance_stats()` switched to route through `metrics.compute_stats()`.** The plan states this extension should be "automatically available to `paper_trading.performance_stats()` and every other caller with zero duplicate math" — but that function was actually calling `backtest.compute_advanced_trade_stats()` directly, bypassing `metrics.py` entirely, so the plan's own claim wasn't yet true. This is a two-line, additive swap (`metrics.compute_stats()` still delegates to the exact same `backtest.compute_advanced_trade_stats()` call for every pre-existing field): `performance_stats()`'s existing callers see every field they already relied on computed identically (regression-tested), plus the two new fields automatically. No circular import risk — `agents.quant_researcher.metrics`/`data_access` have no dependency on `agents.trading_intelligence`.

## Files changed

- **Modified**: `agents/quant_researcher/metrics.py` — `_sortino_ratio()`, `_equity_curve()` helpers, both new keys added in `compute_stats()`.
- **Modified**: `agents/trading_intelligence/paper_trading.py` — `performance_stats()` now delegates to `agents.quant_researcher.metrics.compute_stats()` instead of `backtest.compute_advanced_trade_stats()` directly; docstring updated to match.
- **Modified**: `test_agents/quant_researcher/test_metrics.py` — 15 tests (was 2): the hand-computed-reference tests, the byte-identical-existing-fields regression test, and edge cases (fewer than 2 trades, no downside at all, a trade missing its `"points"` key entirely, empty trade list).
- **Modified**: `test_agents/trading_intelligence/test_paper_trading.py` — 1 new test confirming `performance_stats()` now surfaces both new fields.

No file belonging to Modules 11.1, 11.2, 11.4, or 11.5 was touched.

## Tests executed

- Module suite (`test_metrics.py`): **15/15 passed**.
- `test_agents/trading_intelligence/` + `test_agents/quant_researcher/` combined: **317/317 passed**.
- Full repository suite: **1,360 passed, 1 xfailed** (up from 1,350), **zero regressions**.

Coverage highlights:
- `TestSortinoRatio::test_matches_a_hand_computed_reference_value` / `TestEquityCurve::test_matches_a_hand_computed_reference_sequence`: the plan's own explicit success criterion.
- `test_pre_existing_fields_are_untouched_by_this_module`: the byte-identical-default-behavior guard for every field that existed before this module.
- `TestSortinoRatio`: `None` with fewer than 2 trades, `None` with zero downside variance (all wins), a trade missing its `"points"` key entirely (never raises), and empty trades.
- `TestEquityCurve`: empty-trades edge case, one point per trade in order, and the sanity check that the curve's final value always equals `net_pnl`.
- `test_paper_trading.py::TestPerformanceStats::test_now_also_includes_sortino_ratio_and_equity_curve`: confirms the switch actually surfaces both fields through the real `paper_trading.performance_stats()` call path, on real closed trades.

## Performance impact

Negligible. `_sortino_ratio()` and `_equity_curve()` are both single passes over the same `points` list already derived from `trades` — no new database reads, no new I/O, and `compute_stats()`'s existing callers (Quant Researcher's evolution loop, `paper_trading.performance_stats()`) already pass the full trade list in; nothing is fetched twice.

## Risks

- **`paper_trading.performance_stats()`'s output dict has grown by two keys.** No caller does exact-dict-equality checks on it (verified: only `test_agents/trading_intelligence/test_paper_trading.py` consumes individual keys), so this is additive in practice, not just in principle.
- **Sortino Ratio uses a target=0 (not risk-free-rate-adjusted) semi-deviation**, matching Sharpe Ratio's own existing simplification in this codebase — documented as a deliberate consistency choice, not an oversight; a future module could add a configurable MAR if ever needed, without touching this one.
- **Equity Curve is unbounded in length** (one point per trade) — for a strategy with very many closed trades this could be a large list in the returned dict; no truncation was added since none of the existing size-bounding callers (`limit=10_000` on `list_closed_trades()`) changed, and this matches how `compute_advanced_trade_stats()`'s own inputs are already sized.

## Commit hash

`35fada1` on `worktree-m11-intelligence-depth` (module + tests).

---

Waiting for approval before starting Module 11.7 (per `MILESTONE11_PLAN.md`, M11.7 — Institutional Order-Flow Data Ingestion — is explicitly deferred/out of core scope: it requires a new external data source this project has never ingested. Phase 7 — full validation + docs across all completed modules — is the remaining item in the plan's own roadmap).
