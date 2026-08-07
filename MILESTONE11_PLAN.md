# Milestone 11 — Plan: AI Trading Intelligence Depth & Validation

Status: **planning only** — no production code in this milestone yet. This plan is grounded in a direct survey of the current repository (citations throughout), not invented features. See "Repository survey findings" for the evidence base.

## Vision

Milestone 10 gave BATI a complete, safe, paper-trading-only recommendation pipeline: one snapshot → institutional findings → a fully-reasoned AI recommendation → paper trade → live-recalculated calibration. Milestone 11 does not add a new pipeline — it makes the EXISTING pipeline smarter, more self-aware, and more rigorously validated, by deepening exactly the analytical dimensions the current engine is thinnest on: regime awareness, cross-timeframe confirmation, trade-outcome feedback, and explainability. Every module below extends an existing dataclass, function, or table additively; none replaces the safety invariant, the reuse-over-reimplementation discipline, or the "never fabricate data" rule Milestones 1–10 established.

## Repository survey findings (the evidence base for this plan)

A repository-wide survey (not an assumption) found:

1. **Regime detection is real but thin**: `classify_regime()` (`market_structure.py:67-74`) is a single-indicator (ADX-only), 4-state classifier (TRENDING/RANGING/TRANSITIONING/UNKNOWN), already consumed by `oi_engine.generate_signal()` (`oi_engine.py:558-569,702-823`) and folded into M10's `price_action_reasoning` as text only (`ai_trading_engine.py:313-314`) — never as its own score.
2. **No order-flow data exists beyond OI**: no FII/DII, volume profile, delivery %, or bulk/block-deal data anywhere in the repo. This isn't a code gap — it's a missing external data source, and Angel One's own feed (the only broker session this project ever touches) doesn't appear to provide it either.
3. **Two real self-learning precedents already exist**: `backtest.score_calibration_report()` (`backtest.py:1790-1840`, buckets by `institutional_score`/`institutional_tier`/`regime_at_entry`) and M10's own `ai_trading_engine.calibration_report()` (confidence-bucketed only). Both are honest bucketed-win-rate statistics, explicitly "NOT machine learning" — the established, safe pattern to extend, not replace.
4. **Position sizing has exactly two modes** (`position_sizing.py:31`, `VALID_SIZING_MODES = (None, "fixed", "risk_pct")`) and the module's own docstring **already explicitly rejected** a parallel "ATR-aware sizing mode" as redundant (`position_sizing.py:18-28`, since the SL distance already encodes that). Any M11 sizing work must not re-propose that rejected idea.
5. **No Sortino ratio and no equity-curve time series exist anywhere**, despite Sharpe/win-rate/profit-factor/drawdown/expectancy/recovery-factor all being centrally computed once in `agents/quant_researcher/metrics.compute_stats()` (`metrics.py:16-22`) and reused everywhere else.
6. **A real, unused LLM abstraction already exists**: `agents/llm_providers/` (multi-provider, `generate_with_fallback()`, config-only switching) is used by `dev_agent` for bug detection/patch generation, but `agents/trading_intelligence/` never imports it — M10's reasoning strings are pure templates, not LLM-generated.
7. **`multi_timeframe.py` is purely a data-availability module** (101 lines, `get_timeframe()`/`synchronize()` only) — zero cross-timeframe decision logic exists (nothing checks whether a 3m signal agrees with the 15m/1h trend).

## Objectives

1. Deepen regime awareness from a single ADX threshold to a multi-factor, reusable regime profile.
2. Add real cross-timeframe confirmation to the AI Trading Engine — currently zero exists.
3. Extend probability calibration from confidence-only bucketing to the same multi-dimensional bucketing `backtest.score_calibration_report()` already proved out, using ONLY real closed paper trades.
4. Add a Trade Quality Score for CLOSED trades — a new, honest feedback signal distinct from win/loss, that closes the loop between reasoning quality and outcome.
5. Make AI reasoning optionally richer via the existing LLM abstraction, without ever making it a hard dependency.
6. Add one new, genuinely justified adaptive position-sizing mode (confidence/quality-weighted, NOT the already-rejected ATR-aware idea) plus streak-aware dampening, reusing `risk_engine`'s existing VaR/drawdown primitives.
7. Close the analytics gap (Sortino, equity curve) by extending the one shared `metrics.py`, not duplicating it.
8. Validate all of the above with the same Replay/Stress/Performance/Memory/Trading discipline `test_validation.py` established in Milestone 10.

## System architecture

No new top-level package. Milestone 11 extends `agents/trading_intelligence/` in place, plus one small additive change to `agents/quant_researcher/metrics.py` (new stat, not a new module) and an opt-in import of the existing `agents/llm_providers/`. Data flow is the SAME pipeline from Milestone 10's own architecture diagram (`AI_TRADING_INTELLIGENCE.md`), with three new nodes inserted, all additive:

```
market_data.get_snapshot()
        |
        v
institutional_intelligence.analyze()  <-- deepened: persistence/consistency across cycles (M11 #1)
        |
        v
  [NEW] regime_profile.classify()  -- multi-factor regime, feeds BOTH oi_engine (unchanged
        |                              consumer contract) and ai_trading_engine's reasoning
        v
  [NEW] timeframe_confirmation.check()  -- does 15m/1h agree with the 3m signal? (M11 #2)
        |
        v
ai_trading_engine.evaluate()  <-- Recommendation gains: regime_profile, timeframe_alignment,
        |                          (optional) llm_reasoning; sizing gains a new mode (M11 #5,#6)
        v
paper_trading.enter_from_recommendation()
        |
        v
  [ON CLOSE] trade_quality.score()  -- NEW, computed once per closed trade (M11 #4)
        |
        v
ai_trading_engine.calibration_report()  <-- extended: bucket by regime/timeframe-alignment/
                                             quality too, not just confidence (M11 #3)
```

Every new node is a new, separately-testable function reusing existing primitives — none replaces or forks an existing one. `Recommendation`, `StrikeIntelligence`, and `MarketSnapshot` all gain new OPTIONAL fields with backward-compatible defaults, following the exact discipline M10's own two review passes already used when they added fields.

## Modules

### M11.1 — Regime & Institutional Persistence Engine
Upgrades `classify_regime()`'s single ADX signal into a `RegimeProfile` (trend regime unchanged + a new realized-volatility regime from the ATR history already stored in `market_structure_snapshots`, + an OI-persistence check: is a strike's build-up type sustained across N consecutive cycles, using `data_access.recent_strike_history()` — already built for M10's IV Rank/Premium Momentum). Directly answers the "institutional analysis" priority using data already ingested — no new external feed.
**Backward compatibility**: `classify_regime()`'s existing 4-string return value is UNCHANGED and still consumed by `oi_engine.py` exactly as today; `RegimeProfile` is a new, additive wrapper around it, not a replacement.

### M11.2 — Multi-Timeframe Probability Engine
New `timeframe_confirmation.py`: reuses `multi_timeframe.get_timeframe()`'s already-derived 15m/30m/1h candles (never touches the 1m/5m-unavailable gap) to compute a simple, transparent trend-agreement score (e.g., does the 15m/1h close-over-close direction agree with the 3m signal's direction) — a genuinely new capability, not a duplicate of anything.

### M11.3 — Trade Quality Scoring & Multi-Dimensional Calibration
A `trade_quality.py` module scores each CLOSED trade (0–100, transparent weighted arithmetic matching `strike_intelligence._ai_strike_score()`'s own convention) on how well its reasoning inputs (regime alignment, timeframe confirmation, institutional-finding backing) matched its actual outcome — a genuinely new, honest feedback signal, never a proxy for win/loss alone. `ai_trading_engine.calibration_report()` is extended to accept an optional second bucketing dimension (regime / timeframe-alignment / quality-tier), following the exact multi-dimensional bucketing `backtest.score_calibration_report()` already validated — never a new statistical method.

### M11.4 — Explainable AI Reasoning (LLM-narrated, optional)
A thin, OPTIONAL enrichment: if `agents.llm_providers.is_configured()` (the exact check `security_audit.py` already uses), an LLM turns the existing structured `institutional_reasoning`/`oi_reasoning`/`greeks_reasoning`/`price_action_reasoning` strings into one narrative paragraph via `generate_with_fallback()` — the SAME function `dev_agent` already depends on, so no new LLM integration code is written. If unconfigured, `Recommendation` behaves EXACTLY as it does today (structured strings only) — zero behavior change for any current deployment.

### M11.5 — Adaptive Risk & Position Sizing
One new `VALID_SIZING_MODES` entry, e.g. `"confidence_weighted"` — scales the `risk_pct` mode's own quantity by the Recommendation's own confidence/risk_score (NOT by ATR or structure, which `position_sizing.py` already explicitly and correctly rejected as redundant with the SL-distance denominator). Plus a streak-aware dampener in `ai_trading_engine.py` reading `ti_store.list_closed_trades()`'s own recent history (already queried for calibration) to reduce size after N consecutive losses — reuses `risk_engine.max_drawdown`/`simulate_drawdown_distribution` for its threshold, never a new risk formula.

### M11.6 — Performance Analytics Extension
Adds Sortino ratio and an equity-curve time series to `agents/quant_researcher/metrics.compute_stats()` (the ONE shared definition every stats consumer already uses) — automatically available to `paper_trading.performance_stats()` and every other caller with zero duplicate math.

### M11.7 (deferred / explicitly out of core scope) — Institutional Order-Flow Data Ingestion
FII/DII flow, volume profile, delivery %, bulk/block deals: genuinely valuable, genuinely absent, but requires a NEW external data source this project has never ingested (Angel One's feed doesn't appear to carry it). Deferred to a future milestone rather than fabricated or approximated — the same honesty standard M10 held 1m/5m candles to.

## Development phases

| Phase | Module(s) | Depends on |
|---|---|---|
| 1 | M11.1 Regime & Institutional Persistence | M10 (`market_structure_snapshots`, `data_access.recent_strike_history`) only |
| 2 | M11.2 Multi-Timeframe Probability | Phase 1 (regime profile feeds the confirmation reasoning text), M10's `multi_timeframe.py` |
| 3 | M11.6 Performance Analytics Extension | None beyond M10 — can run in parallel with Phase 1/2 |
| 4 | M11.5 Adaptive Risk & Position Sizing | Phase 1 (regime informs the streak-dampener's threshold context) |
| 5 | M11.3 Trade Quality Scoring & Multi-Dimensional Calibration | Phases 1, 2, 4 (needs their outputs as bucketing dimensions) AND real elapsed time for paper trades to close in volume — a genuine TIME dependency, not just code |
| 6 | M11.4 Explainable AI Reasoning | None beyond M10 (`agents/llm_providers/` already exists) — can run any time, including in parallel with Phase 1 |
| 7 | Full validation + docs | All of the above |

Phases 3 and 6 have no hard dependency on 1/2/4/5 and can be pulled forward if desired; Phase 5 is the one phase that cannot be meaningfully validated until Phases 1/2/4 have been running long enough to accumulate real closed trades — this is flagged explicitly rather than glossed over, matching M10's own "probability calibration starts empty" honesty.

## Dependencies

- **Internal**: `oi_engine.py`, `market_structure.py`, `greeks.py` (read-only reuse, unchanged); `agents.risk_manager.risk_engine` (VaR/drawdown primitives, reused not reinvented); `agents.quant_researcher.metrics` (extended in place); `agents.llm_providers` (reused, not rebuilt); `ti_store`/`data_access` (both already have everything Phase 1/2/3 need — no new tables required except one small addition: a `trade_quality_score` column on `ti_paper_trades`, or a parallel `ti_trade_quality` table, decision deferred to Phase 5's own design step).
- **External**: none required for Phases 1, 2, 3, 5 core, 6 core. Phase 4 requires an LLM provider to be configured (already optional/degradable by design). M11.7 (deferred) would require a new external data vendor — explicitly not committed to in this plan.
- **Data volume**: Phase 5's calibration enhancements need a real, growing sample of closed paper trades — this depends on the M10 scheduler (already wired, 3-minute cadence, market-hours gated) running for real elapsed time, not on any code in M11.

## Testing & validation strategy

Mirrors Milestone 10's own established convention exactly, not a new methodology:
- `ti_db`-style fixtures + `insert_realistic_chain()` for every new module's unit tests (same `test_agents/trading_intelligence/conftest.py`).
- `test_safety.py`'s AST scan extended to cover every new file automatically (it already scans the whole package, not a fixed file list).
- Every new dataclass field is additive with a safe default — a regression test per module asserts existing callers/tests are unaffected (the same discipline that caught the T1/targets and strike=None bugs in M10's own review passes).
- A `test_validation.py`-style Replay/Stress/Performance/Memory/Trading suite extended per phase, not written once at the end — Phase 7 aggregates and re-runs the full existing suite (currently 1,220 tests) plus every new phase's additions.
- Full repository suite re-run after every phase, zero-regression gate before moving to the next phase (matching the two-full-suite-reruns-per-review-pass pattern already used for M10).

## Risks

| Risk | Mitigation |
|---|---|
| Regime/timeframe complexity creep beyond what's testable | Each new score is transparent, documented arithmetic (M10's own established convention) — no fitted models, no new statistical methods beyond the two already-proven bucketing patterns |
| Calibration overfitting on a small trade sample | Same `CALIBRATION_MIN_SAMPLE` honesty gate M10 already established, applied to every new bucketing dimension — honestly `None` below threshold, never fabricated |
| LLM cost/latency/availability | Fully optional path (M11.4), degrades to M10's existing structured strings with zero behavior change if unconfigured — same pattern `security_audit.py` already uses for `is_configured()` gating |
| Re-proposing the already-rejected "ATR-aware sizing mode" | M11.5 is explicitly scoped to confidence/quality-weighted sizing, a genuinely different (and not previously rejected) input — documented distinction included in the module's own design |
| Increased per-cycle computation load on the M9 scheduler | Every new score reuses already-fetched data (no new DB reads beyond what M10 already performs per cycle) — Phase 7 includes a performance-budget regression test, the same pattern `test_validation.py::TestPerformance` already established |
| Backward compatibility breakage | Every field addition follows M10's own proven pattern (new optional fields, safe defaults, existing tests re-run unmodified) rather than restructuring any existing dataclass |
| Fabricating institutional order-flow data to "complete" the feature set | Explicitly deferred (M11.7) rather than approximated — matches the project's 1m/5m honesty precedent |

## Success criteria (measurable, per module)

1. **M11.1**: `RegimeProfile` computed for every symbol every cycle with zero exceptions across a 20+ cycle replay test (same shape as M10's own `TestReplay`); existing `classify_regime()` callers and their tests remain unmodified and passing.
2. **M11.2**: Timeframe-alignment score present on every BUY recommendation; a stress test across a wide multi-strike chain confirms it never raises when 15m/1h data is unavailable (honest degradation, not a crash).
3. **M11.3**: Trade Quality Score computed for 100% of closed trades in a lifecycle test; `calibration_report()` accepts an optional second dimension without changing its existing confidence-only output shape (regression-tested).
4. **M11.4**: Zero behavior change (byte-identical `Recommendation` output) when `agents.llm_providers.is_configured()` is False, verified by a dedicated test; narrative reasoning present and non-empty when a fake/mocked provider is configured.
5. **M11.5**: New sizing mode's output quantity is provably bounded by the same max-loss guarantee `risk_pct` mode already has (a direct comparison test, not just a smoke test); streak dampener reduces size only after a real, test-verified losing streak, never fabricated.
6. **M11.6**: Sortino ratio and equity curve match a hand-computed reference value on a fixed synthetic trade sequence (the same "verify against a known-correct hand calculation" discipline `metrics.py`'s existing tests already use).
7. **Overall**: full repository suite (1,220+ tests today) remains 100% green after every phase; no previous milestone's file is modified beyond the additive touch-points listed in "System architecture."

## Estimated implementation effort

Sized relative to Milestone 10's own already-measured modules (10 files / 1,850 lines of production code / 13 test files / 1,332 lines of tests, built in two passes), not invented hour estimates:

| Phase | Relative size | Comparable to |
|---|---|---|
| 1 (Regime & Institutional Persistence) | Medium | ~M10's `institutional_intelligence.py` (274 lines + ~15 tests) |
| 2 (Multi-Timeframe Probability) | Small–Medium | ~M10's `multi_timeframe.py` extended (~150 new lines + ~10 tests) |
| 3 (Trade Quality & Calibration) | Medium | ~M10's `strike_intelligence.py` scoring additions (~200 lines + ~15 tests) |
| 4 (Explainable AI) | Small | A thin wrapper around existing `llm_providers` (~80 lines + ~8 tests, mocked LLM) |
| 5 (Adaptive Sizing) | Small | ~M10's `_compute_risk_score()` scope (~100 lines + ~10 tests) |
| 6 (Performance Analytics) | Small | Additive `metrics.py` functions (~60 lines + ~8 tests) |
| 7 (Validation + docs) | Medium | ~M10's own `test_validation.py` + two review-pass docs |

Total: comparable in scope to Milestone 10 itself (which took two build passes plus two review passes) — planned as a similarly-staged rollout (build → review → final review) rather than one monolithic pass, for the same reason M10's own two-pass review process caught 4 real bugs a single pass would likely have missed.
