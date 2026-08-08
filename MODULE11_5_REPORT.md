# Module 11.5 Report — Adaptive Risk & Position Sizing

## Objective

Give position sizing access to real, already-computed BATI evidence — Regime Profile (Module 11.1), Multi-Timeframe Alignment (Module 11.2), Institutional Persistence, Trade Quality Score (Module 11.3), and the existing Risk Manager — so a trade's size reflects how well-supported its entry actually is and how this engine's own recent track record has been behaving, without ever fabricating confidence or inventing a new risk formula.

## Architecture

**Layered on top of, never replacing, `position_sizing.compute_quantity(sizing_mode="risk_pct")`.** The new `agents/trading_intelligence/adaptive_sizing.py` module computes the exact same `risk_pct` base quantity first (unchanged formula, unchanged call), then applies up to three real, evidence-based multipliers — all bounded in `(0, 1.0]`, so the result can only ever be scaled **down** from that baseline, never up:

```
base_qty = position_sizing.compute_quantity(entry, sl, sizing_mode="risk_pct", capital=capital, risk_pct=risk_pct)
qty = min(base_qty, round(base_qty * setup_multiplier * track_record_multiplier * streak_multiplier))
```

1. **Setup-strength multiplier** — averages whichever of regime alignment (`trade_quality.REGIME_TREND_SCORE`, reused directly), timeframe alignment score, and institutional backing (100/0) are actually available for this entry, then maps 0–100 linearly onto `[0.5, 1.0]`. No evidence at all → neutral `1.0` (never a fabricated reduction).

2. **Quality-tier track-record multiplier** — looks up this engine's own historical win rate for trades whose *setup* fell in the same `trade_quality.quality_tier()` band as the current one, gated by a `TRACK_RECORD_MIN_SAMPLE = 5` honesty threshold (insufficient sample → neutral `1.0`, never a guessed win rate). Below-neutral (< 50%) win rates scale the multiplier down toward `MIN_TRACK_RECORD_MULTIPLIER = 0.5`; at-or-above-neutral win rates stay at `1.0` (never scaled up).

3. **Streak-aware dampener** — walks `ti_store.list_closed_trades()` for the current unbroken losing streak, then compares its cumulative loss against the worse of `risk_engine.max_drawdown()` and `risk_engine.simulate_drawdown_distribution()`'s percentile, both computed on this engine's **prior** history only (everything before the streak began — the streak itself never inflates its own baseline). Reaching that threshold applies `STREAK_DAMPENER_FACTOR = 0.5`; otherwise `1.0`.

**Why `position_sizing.py` itself is untouched**: that module is explicitly shared with Exit Engine V4's backtest replay and its own docstring already rejects folding structure/ATR-specific knowledge into it for exactly this reason. Threading regime/timeframe/institutional context into it would require it to depend on `agents.trading_intelligence`, the wrong direction. `adaptive_sizing.py` depends on `position_sizing.py`, never the reverse — zero risk to V4's existing usage.

**Wiring**: `ai_trading_engine.evaluate()` gains an optional `sizing_mode: str = "risk_pct"` parameter. The default is byte-identical to before this module (verified by a dedicated regression test comparing `evaluate()`'s output directly against an explicit `position_sizing.compute_quantity()` call); passing `sizing_mode="adaptive"` routes through `adaptive_sizing.compute_adaptive_quantity()` instead, reusing the `regime_profile`/`timeframe_confirmation`/`trade_quality.institutional_backing()` reads with the same `snapshot`/`market_structure`/`findings` dedup discipline Module 11.3 already established.

**Two real design issues found and fixed during testing, before commit:**

- Bucketing historical trades by their **final** `trade_quality.score()` tier (which already bakes in `outcome_alignment` — whether the trade's confidence direction matched its outcome) silently mixed real wins together with "correctly-anticipated losses" into the same HIGH bucket, since both score highly under that formula. A test built to prove a poor track record reduces sizing instead returned neutral, exposing the bug. Fixed by tiering on `TradeQualityScore.setup_strength` (the pre-outcome signal) instead — this answers the question sizing actually needs: "historically, when the entry looked this strong, what fraction of those trades won?"
- An initial streak-dampener design bootstrapped the **entire** history including the current streak's own large losses. Because with-replacement resampling can draw a rare large loss *more times* than it actually occurred, the resulting percentile threshold was almost always higher than any real, bounded streak could produce — the dampener essentially never tripped for realistic scenarios (confirmed empirically across dozens of RNG seeds and loss magnitudes before the fix). Redesigned to compare the streak against `risk_engine` primitives computed on **prior** history only; the fix was verified robust across five different RNG seeds in both the trip and no-trip regression tests, not tuned to one lucky seed.

## Files changed

- **New**: `agents/trading_intelligence/adaptive_sizing.py` — `AdaptiveSizingResult` dataclass, `compute_adaptive_quantity()`, and the setup-strength/track-record/streak-dampener helpers.
- **New**: `test_agents/trading_intelligence/test_adaptive_sizing.py` (29 tests).
- **Modified**: `agents/trading_intelligence/ai_trading_engine.py` — `evaluate()` gains `sizing_mode`, module-level `SIZING_MODES` constant, `regime_profile`/`adaptive_sizing` imports; the `findings` resolution block was moved a few lines earlier (before quantity computation, not after) so the adaptive path can reuse it without a second institutional-intelligence sweep — a pure reordering with no behavioral change to the default path, covered by the unchanged-default regression test.
- **Modified**: `test_agents/trading_intelligence/test_ai_trading_engine.py` — `TestSizingMode` (5 tests).

No file belonging to Modules 11.1–11.4 (`regime_profile.py`, `timeframe_confirmation.py`, `trade_quality.py`, `explainability.py`) was touched.

## Tests executed

- Module suite: **29/29 passed**.
- Full `test_agents/trading_intelligence/` suite: **248/248 passed** (up from 215).
- Full repository suite: **1,350 passed, 1 xfailed** (up from 1,317), **zero regressions**.

Coverage highlights:
- `TestSetupStrength` / `TestSetupMultiplier`: every combination of available/unavailable components, the `MAX_SETUP_MULTIPLIER <= 1.0` invariant asserted directly.
- `TestTrackRecordMultiplier`: insufficient-sample honesty gate, a strong track record staying neutral (never scaling above 1.0), a poor track record reducing size — the exact scenario that caught the setup_strength-vs-score tiering bug.
- `TestCurrentLosingStreak` / `TestStreakDampenerMultiplier`: streak detection edge cases, insufficient-history inactivity, and — the plan's own explicit success criterion — a real, test-verified losing streak tripping the dampener and a mild streak not tripping it, **both re-run across 5 RNG seeds** to prove the fix isn't seed-fragile.
- `TestComputeAdaptiveQuantity::test_never_exceeds_the_risk_pct_baseline_max_loss_guarantee`: the plan's own Module 11.5 success criterion verbatim — a direct comparison against `position_sizing.compute_quantity()`'s own output across five regime/alignment/institutional scenarios including the maximally favorable one, asserting both quantity and implied max loss stay bounded.
- `TestAdaptiveSizingIntegration`: end-to-end against real archived NIFTY data (real regime + real timeframe reads).
- `TestSizingMode` (`test_ai_trading_engine.py`): the default-unchanged regression guard, invalid-mode rejection, and an end-to-end `evaluate(..., sizing_mode="adaptive")` run against a real generated BUY CE signal.

## Performance impact

The `"risk_pct"` (default) path is completely unchanged — same single `position_sizing.compute_quantity()` call, same location in `evaluate()`. The `"adaptive"` path (opt-in only) adds, per trade actually about to be sized: one `regime_profile.classify()` call (reusing the already-fetched `market_structure` local variable — no extra DB read for that part), one `timeframe_confirmation.check()` call, one `trade_quality.institutional_backing()` call (reusing the already-resolved `findings` list, no second institutional-intelligence sweep), and — inside `adaptive_sizing` — one `ti_store.list_closed_trades()` read reused across both the track-record and streak-dampener calculations, plus one `risk_engine.simulate_drawdown_distribution()` bootstrap (500 trials by default, pure in-memory arithmetic, no I/O). This only runs when `evaluate()` is about to size an actionable BUY signal, not on every cycle.

## Risks

- **`MIN_SETUP_MULTIPLIER = 0.5`, `MIN_TRACK_RECORD_MULTIPLIER = 0.5`, `STREAK_DAMPENER_FACTOR = 0.5`, and `STREAK_DAMPENER_PERCENTILE = 75` are documented, transparent, configurable constants — not fitted or backtested values.** If real usage shows they need adjustment, that's a constant change, not a rewrite; all are named module-level constants per the "configurable risk limits" requirement.
- **The streak dampener requires real prior history (`STREAK_DAMPENER_MIN_TRADES = 10` before AND after excluding the current streak)** — a genuinely new engine with few closed trades gets no dampening at all, by design (no fabricated threshold), the same "starts empty, never fabricated" honesty already established for the Probability calibration framework.
- **Not yet the default** — `sizing_mode="adaptive"` is fully opt-in; enabling it in production (e.g. wiring it into `api.run_scheduled_cycle()`) is a separate decision not made by this module, matching the "one module at a time" discipline.
- **The bootstrap-based streak threshold uses real randomness in production** (`rng=None`) — this is the same accepted pattern `risk_engine.simulate_drawdown_distribution()` already uses elsewhere (e.g. the promotion gate); tests pin a seeded `random.Random` for reproducibility, verified across multiple seeds rather than one.

## Commit hashes

`0726fc9` on `worktree-m11-intelligence-depth` (module + tests).

---

Waiting for approval before starting Module 11.6 (Performance Analytics Extension).
