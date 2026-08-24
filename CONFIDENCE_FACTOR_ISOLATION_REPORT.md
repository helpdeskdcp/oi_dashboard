# Confidence Factor Isolation — Backtest Report

**Status: no code change. Of the 4 confidence-scoring factors this backtest path can actually exercise, none shows a clean positive edge -- and the single most-frequently-firing one (PCR extremity, active on 78% of all trades) is associated with measurably worse outcomes, not better. A further 5 of the ~9 live scoring factors could not be tested at all: `backtest.py`'s `simulate_trades()` structurally never supplies the inputs they require, so they are dead weight in every backtest run so far, including the three prior reports in this series.**

## Why this report exists

`ENTRY_BIAS_SELECTION_REPORT.md` found the aggregate confidence score has
essentially zero correlation with trade outcome (-0.0115) and flagged, as
its own explicit next step, testing each of `generate_signal()`'s
individual confidence bonuses/penalties in isolation rather than only the
combined score -- one might carry real signal even though the aggregate
doesn't. This report is that test, and it surfaced a more important,
structural finding along the way.

## A prerequisite finding: 5 of 9 factors were never actually tested

`generate_signal()` has up to nine independent confidence
bonuses/penalties. Checking exactly what `backtest.simulate_trades()`
passes it (the call every report in this series has relied on):

```python
signal = generate_signal(
    rows, atm, bias, c["note"], pcr, support, resistance,
    target_delta_approx=TARGET_DELTA_APPROX, sl_percent=OLD_ENGINE_SL_PERCENT,
    min_target_percent=MIN_TARGET_PERCENT, confidence_threshold=confidence_threshold,
    candles=recent_candles, momentum_confirmation_enabled=momentum_confirmation_enabled,
    momentum_bonus=momentum_bonus, momentum_penalty=momentum_penalty,
)
```

`nse_atm_row`, `underlying`, `expiry_date`, and `market_structure` are
never passed -- they default to `None`. Five of the nine scoring
mechanisms are gated on exactly those parameters:

| Factor | Gate | Fires in backtest? |
|---|---|---|
| Structural proximity bonus/penalty | `if market_structure and underlying:` | **Never** (`market_structure=None`) |
| Regime alignment bonus/penalty | `if market_structure and market_structure.get("regime")...` | **Never** |
| Cross-verify-wall bonus | `cross_verify_wall(wall_strike, direction, market_structure or {})` | **Never** (always gets `{}`, never a match) |
| Dual-source agreement bonus/penalty | `if nse_atm_row is not None:` | **Never** |
| Order-flow imbalance bonus/penalty | `if nse_atm_row is not None:` | **Never** |

This means `ENTRY_SL_TARGET_BACKTEST_REPORT.md`, `SL_TARGET_RETUNE_REPORT.md`,
and `ENTRY_BIAS_SELECTION_REPORT.md` all backtested a confidence score
built from only **4 of the 9 factors live trading actually uses**. Those
reports' conclusions (weak real-world performance, no edge from
retuning exit parameters, ~0 correlation between confidence and outcome)
are real and correctly evidenced for those 4 factors -- but they say
nothing about whether the other 5 (which only ever fire live, where real
`market_structure`/NSE order-book data exists) carry genuine signal.
That's a real gap in this backtest path, not a defect in the live code,
and it's flagged as the clearest actionable follow-up below.

## The 4 factors that could be tested

For every real trade `simulate_trades()` generated (2,405 total, same
archive as every prior report), the corresponding cycle's raw inputs
(PCR, OI signal-field, CE/PE volume, bias text) were used to determine
whether each factor's condition held at signal time, independent of
`generate_signal()`'s own internals -- then trades were split into
"fired" vs. "not fired" and compared with `compute_advanced_trade_stats()`.

| Factor | Group | Trades | Resolved | Accuracy | Profit Factor | Net Points | Expectancy |
|---|---|---:|---:|---:|---:|---:|---:|
| **PCR extremity** (>1.3 or <0.7) | FIRED | 1,872 | 252 | 21.4% | **0.53** | -17,047.60 | -9.11 |
| | not fired | 533 | 93 | 26.9% | 0.67 | -2,342.30 | -4.39 |
| **OI signal-field** (Short Covering/Buildup) | FIRED | 198 | 27 | 25.9% | 0.68 | -733.95 | -3.71 |
| | not fired | 2,207 | 318 | 22.6% | 0.55 | -18,655.95 | -8.45 |
| **Volume dominance** (>1.2x opposite side) | FIRED | 795 | 141 | 32.6% | 0.51 | -8,154.05 | -10.26 |
| | not fired | 1,610 | 204 | 16.2% | 0.59 | -11,235.85 | -6.98 |
| **Breakout/breakdown bias text** | FIRED | 25 | 5 | n<20 (raw 40.0%) | 0.70 | -158.65 | -6.35 |
| | not fired | 2,380 | 340 | 22.6% | 0.55 | -19,231.25 | -8.08 |

## Finding

**None of the 4 testable factors shows a clean, trustworthy positive
edge**, and the most consequential one runs backwards:

- **PCR extremity fires on 78% of all trades** (1,872/2,405) -- it is by
  far the dominant driver of the confidence score's variance. Trades
  where it fired performed measurably *worse* (profit factor 0.53 vs.
  0.67, expectancy -9.11 vs. -4.39 pts) than trades where it didn't. A
  bonus this influential, running in the wrong direction, is the largest
  single contributor to the ~0 correlation `ENTRY_BIAS_SELECTION_REPORT.md`
  found in the aggregate score.
- **Volume dominance** repeats the same pattern seen in that report's
  confidence-bucket table: higher accuracy (32.6% vs. 16.2%) paired with
  a *worse* profit factor (0.51 vs. 0.59) -- more, smaller wins offset by
  bigger losses, not a net improvement.
- **OI signal-field** is the only factor pointing the right direction
  (0.68 vs. 0.55), and the least influential (fires on only 8% of
  trades) -- 27 resolved trades is just above this report series' own
  20-resolved honesty floor, worth further validation, not yet a
  confirmed edge.
- **Breakout/breakdown bias text** is too rare in this dataset (25
  trades, 5 resolved) to say anything trustworthy either way.

None of this is a coding bug -- every factor computes exactly what its
own docstring says it does. The finding is that, on this archive, the
*weights* attached to these conditions don't reflect their actual
predictive value -- PCR extremity in particular would need to be
inverted or removed to stop actively hurting the score, and that
conclusion itself would need its own held-out validation before any
change, matching every prior report's discipline.

## What this report does NOT do

No weight, threshold, or formula changed. Flipping or removing the PCR
bonus based on one archive's backtest, without an out-of-sample check,
would repeat the exact overfitting mistake `SL_TARGET_RETUNE_REPORT.md`
already caught once in this series.

## What's next (not started here)

- **Extend the backtest to actually exercise the other 5 factors.**
  `market_structure_snapshots` may already be archived (the same table
  `analyze_v2_signals()` reads for Engine V2's own backtest) -- if a real
  per-cycle `market_structure` can be reconstructed and joined into
  `simulate_trades()`'s replay the same way, the structural/regime/
  cross-verify bonuses could finally be backtested instead of assumed.
  NSE order-book depth for `nse_atm_row` may not be archived at all;
  worth checking before assuming it's recoverable.
- If PCR extremity's inverse relationship holds up under a proper
  train/test split (same methodology as `SL_TARGET_RETUNE_REPORT.md`),
  that's a concrete, evidence-backed candidate change -- but it needs
  that validation first, not a direct edit.
