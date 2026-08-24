# Probability Calibration Audit

**Status: the current confidence score is NOT calibrated to P(target before SL), and the codebase already correctly avoids calling it "probability" for exactly this reason. No code changed by this document.**

## What "calibrated" means here, precisely

Per the user's own framing: probability must mean *calibrated probability of TARGET being reached before SL*, evaluated by comparing predicted confidence buckets against the actual empirical target-before-SL rate among historical setups in that bucket. If a bucket implies 70-80% and the real rate is 52%, the model is not calibrated. This audit runs exactly that comparison, using the REAL production bucket boundaries from `ai_trading_engine.CALIBRATION_BUCKETS = ((0,39),(40,59),(60,79),(80,100))` and `CALIBRATION_MIN_SAMPLE = 5` (`agents/trading_intelligence/ai_trading_engine.py:80-81`) — not an ad-hoc bucketing invented for this report.

## Method

All 2,405 trades from the same full-archive backtest used throughout this report series (2026-07-13 to 2026-08-21, all 11 watched symbols, `backtest.simulate_trades()`), bucketed by `confidence` at entry, `P(target before SL)` computed as `wins / (wins + losses)` among each bucket's resolved (`TARGET HIT`/`STOP LOSS`) trades — the same "accuracy" definition `backtest.compute_advanced_trade_stats()` already uses elsewhere in this report series, applied per-bucket here.

Note: this tests whether `_calibrated_probability()`'s bucketing *mechanism* would produce an accurate figure if it had this much history to draw on. The real `ti_paper_trades` table has nowhere near enough closed trades for a genuine live-history calibration check — this backtest archive stands in as a much larger synthetic history for that purpose.

## Results

| Bucket | Trades | Resolved | Target hits | SL hits | P(target before SL) | Bucket implies | Calibrated? |
|---|---:|---:|---:|---:|---:|---:|---|
| 0-39 | 0 | 0 | 0 | 0 | -- | 0-39% | n/a — structurally unreachable (see below) |
| 40-59 | 0 | 0 | 0 | 0 | -- | 40-59% | n/a — structurally unreachable (see below) |
| **60-79** | 2,310 | 325 | 74 | 251 | **22.8%** | 60-79% | **NO** — 22.8% actual vs. 60-79% implied |
| **80-100** | 95 | 20 | 5 | 15 | **25.0%** | 80-100% | **NO** — 25.0% actual vs. 80-100% implied |

**The two lowest buckets are structurally unreachable**, not merely unobserved: `generate_signal()`'s `tradeable` field (which gates whether `simulate_trades()` ever opens a position) requires `confidence >= confidence_threshold` (60 throughout this report series), so no trade with confidence below 60 can ever appear in this data. This is worth noting as its own finding: the calibration bucket scheme (`0-39`/`40-59`/`60-79`/`80-100`) was defined without apparent reference to the fact that the entry gate itself makes the two lower buckets permanently empty in practice.

**Both populated buckets are badly miscalibrated, in the same direction**: both show roughly 23-25% real target-before-SL rate regardless of whether the predicted bucket says "60-79%" or "80-100%" — the score doesn't just fail to hit its implied rate, it barely discriminates between "high" and "very high" confidence at all. This is consistent with, and quantifies precisely, `ENTRY_BIAS_SELECTION_REPORT.md`'s already-established finding that `confidence` and outcome are essentially uncorrelated (Pearson -0.0115).

## What the codebase already does right

`Recommendation.confidence` (the raw `generate_signal()` score) and `Recommendation.probability` (`_calibrated_probability()`'s own real historical win-rate-by-bucket calculation) are **already two distinct, never-conflated fields** — `_calibrated_probability()` does not treat the confidence score itself as a probability; it computes a genuinely separate, historically-grounded number, and returns `None` with an honest "insufficient history" note below `CALIBRATION_MIN_SAMPLE=5` rather than fabricating a percentage. This satisfies the user's "do not force confidence to equal probability" principle by construction, without any change needed here.

**What this audit adds**: confirms that even the *mechanism* `_calibrated_probability()` uses — win-rate-by-confidence-bucket — would, if it had a large enough real history to draw on, report a number far below what the bucket boundaries themselves imply. The mechanism's honesty (never fabricating below the sample floor) is real and correct; its practical output, once enough data exists to compute one, would not currently earn the label "70-80% probability" in any meaningful sense — it would show roughly a 1-in-4 target-before-SL rate regardless of which of the two reachable buckets a signal fell into.

## Conclusion

**Not calibrated.** The gap between implied and actual rates (60-79% implied vs. 22.8% actual; 80-100% implied vs. 25.0% actual) is large and consistent across both populated buckets, not a marginal miscalibration. Per the user's own instruction, this is documented as a finding, not corrected here — any correction (retuning bucket boundaries, rebuilding the underlying confidence formula, or adopting a genuinely different calibration approach such as the already-built-but-unwired `dual_probability_*` module) needs its own dedicated, out-of-sample-validated effort, not a parameter tweak bundled into this audit.
