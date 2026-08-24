# Expected Value Gate — Design (Not Implemented)

**Finding: no module computes an explicit EV gate today, and the one real probability-shaped number that exists (`ai_trading_engine._calibrated_probability()`) is not calibrated to the specific event an EV formula needs — building the gate on it now would be false precision on top of an already-known-uncalibrated number (see `PROBABILITY_CALIBRATION_AUDIT.md`). No code changed by this document.**

## What exists today that could feed P_target

`ai_trading_engine._calibrated_probability()` (line 175) is the only real probability-shaped output in the canonical path. Given a `confidence` score, it buckets into `CALIBRATION_BUCKETS = ((0,39),(40,59),(60,79),(80,100))`, pulls closed trades from `ti_store.list_closed_trades()` in that bucket, and returns `wins / len(trades)` as a percentage — with an honest `None` + note when fewer than `CALIBRATION_MIN_SAMPLE = 5` trades exist in the bucket. This is a genuine historical win-rate, not a fabricated number, and it correctly degrades to "insufficient history" rather than guessing.

**The problem**: `PROBABILITY_CALIBRATION_AUDIT.md` (this same PR) measured that this bucketing mechanism, run against a large synthetic history, reports roughly 23-25% real target-before-SL rate regardless of whether the bucket implies "60-79%" or "80-100%." A win-rate computed per bucket of this confidence score is not meaningfully calibrated to "P(target before SL) given the real setup features." Wiring an EV formula on top of it today would produce a precise-looking number with no more real predictive value than the confidence score it's built from.

## Proposed structure (pseudocode, not code)

```
function compute_expected_value(P_target, P_SL, reward_points, risk_points):
    assert P_target is not None and P_SL is not None   # never silently default to a guessed probability
    if abs(P_target + P_SL - 1.0) > tolerance:
        # outcomes are not being treated as mutually exclusive (e.g. a
        # third TIME_EXIT/EXPIRED outcome exists, as BACKTEST_PAPER_LIVE_EXIT_CONTRACT.md
        # establishes it does for the live path) -- do NOT force the
        # two-outcome equation; return None with a reason.
        return None, "P_target + P_SL != 1 -- outcome space is not two-way, EV undefined under this formula"
    return (P_target * reward_points) - (P_SL * risk_points), None
```

Inputs: `P_target`/`P_SL` (calibrated probabilities, NOT the raw confidence score), `reward_points = target_price - entry_price`, `risk_points = entry_price - sl_price` (both already computed today by `generate_signal()`). Output: a signed points value, or `None` with an explicit reason when the required calibration doesn't exist yet.

**Important consequence of the exit-contract finding**: the live path's real trade outcomes are `TARGET HIT` / `STOP LOSS` / `EXPIRED (rollover)`, not a clean two-way split — and with no time-exit boundary, there's no `TIME_EXIT` bucket at all live (unlike backtest). This means `P_target + P_SL = 1` is not automatically a safe assumption for the live path today; the pseudocode above deliberately checks rather than assumes it.

## What would need to be true before this gate is worth building for real

1. A probability estimate that has actually been validated to predict `target before SL` specifically (not a relabeled confidence score) — the existing `dual_probability_*` module (`ARCHITECTURE_AUDIT.md`: built, never wired in, target model never validated at any horizon per `DUAL_PROBABILITY_CALIBRATION_REPORT.md`) is the closest existing attempt, and it's also not there yet.
2. A resolved definition of the outcome space for whichever path (backtest vs. live) the gate runs against, given the exit-contract mismatch above.
3. Its own before/after backtest validation once built, same as every other signal-affecting change in this repo.

**Recommendation: do not build this gate until (1) is resolved.** Building it now on top of a known-uncalibrated confidence score would not meet the "do not invent P values" bar — it would be inventing one by relabeling.
