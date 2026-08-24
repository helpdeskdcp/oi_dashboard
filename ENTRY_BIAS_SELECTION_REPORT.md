# Entry / Bias Selection — Investigation Report

**Status: no code change. This is the root-cause finding the two prior reports (`ENTRY_SL_TARGET_BACKTEST_REPORT.md`, `SL_TARGET_RETUNE_REPORT.md`) were pointing toward. `oi_engine.detect_bias()`'s directional call, evaluated the most direct way possible, scores 49.9% on a binary up/down prediction across 2,303 real signals -- a coin flip scores ~50%. The confidence score layered on top of it shows essentially zero correlation with actual trade outcomes. Neither finding is about a coding bug; both are about whether the underlying strategy logic has a real edge, which is a decision for the operator, not something fixed by more backtesting alone.**

## Why this report exists

`SL_TARGET_RETUNE_REPORT.md`'s own closing read: with SL/target/hold-time
all clustered in a narrow 0.57-0.92 profit-factor band no matter how those
exit parameters were varied, the likelier bottleneck is **which trades get
taken** (entry/bias selection), not **how they're managed once taken**
(exit sizing). This report investigates that directly, with two
independent, non-invasive checks against the same real ~6-week archive
(`2026-07-13` to `2026-08-21`, all 11 `TI_WATCHED_SYMBOLS`) used by every
prior report in this series.

## Check 1 -- Confidence calibration

If `generate_signal()`'s ~8 confidence bonuses/penalties (PCR extremity,
OI signal-field, volume dominance, breakout/breakdown bias, structural
proximity, regime alignment, dual-source agreement, order-flow imbalance)
carry real signal, trades with a higher resulting `confidence` should win
more often and by more. Bucketing all 2,405 real backtested trades by
their own `confidence` field:

| Confidence | Trades | Resolved | Accuracy (excl. time exits) | Profit Factor | Net Points |
|---|---:|---:|---:|---:|---:|
| 60-69 | 1,956 | 274 | 19.7% | 0.61 | -12,606.50 |
| 70-79 | 354 | 51 | 39.2% | 0.37 | -6,265.25 |
| 80-89 | 73 | 14 | n<20 (raw 14.3%) | 0.62 | -540.35 |
| 90-100 | 22 | 6 | n<20 (raw 50.0%) | 1.08 | +22.20 |

No monotonic pattern: the 70-79 bucket has *higher* raw accuracy than
60-69 but a *worse* profit factor (bigger losses relative to wins). The
90-100 bucket looks best, but at 6 resolved trades it's far below this
project's own 20-resolved honesty floor -- not a result to act on.

**The more robust check**: Pearson correlation between `confidence` and
points earned, across all 2,405 trades: **-0.0115** -- indistinguishable
from zero. A confidence score that meant anything would show a positive
correlation; this one shows none. The confidence distribution itself is
also telling: 1,956 of 2,405 trades (81%) score exactly 60 or 65 -- the
scoring bonuses rarely stack meaningfully in either direction.

## Check 2 -- Directional accuracy of the bias call itself

Check 1 tests the confidence *score*. This checks the underlying
*direction* call (`detect_bias()` -> `BULLISH`/`BEARISH` -> `BUY CE`/
`BUY PE`) on its own terms, deliberately stripped of every other moving
part -- premium, delta, IV, OI-wall projection, SL/target mechanics all
introduce their own noise on top of the direction call. For every real
signal `simulate_trades()` generated, this checks only: **did the
underlying itself move in the predicted direction within the next 15
minutes** (about half of `MAX_HOLD_MINUTES`), using the archived
`underlying_ltp` series directly, no lookahead (only cycles at-or-after
each trade's own entry timestamp are used to check its own outcome).

| Symbol | Correct | Total | Directional Accuracy |
|---|---:|---:|---:|
| NIFTY | 61 | 114 | 53.5% |
| BANKNIFTY | 66 | 151 | 43.7% |
| SENSEX | 14 | 21 | 66.7% (n too small to trust) |
| NATURALGAS | 236 | 477 | 49.5% |
| NATGASMINI | 248 | 482 | 51.5% |
| CRUDEOIL | 77 | 142 | 54.2% |
| CRUDEOILM | 233 | 464 | 50.2% |
| GOLD | 62 | 126 | 49.2% |
| GOLDM | 70 | 156 | 44.9% |
| SILVER | 23 | 59 | 39.0% |
| SILVERM | 60 | 111 | 54.1% |
| **OVERALL** | **1,150** | **2,303** | **49.9%** |

A coin flip scores ~50%. **49.9% across 2,303 real signals is a coin
flip.** No symbol shows a robust, large-sample edge in either direction --
the per-symbol range (39.0% to 66.7%) is consistent with sampling noise
around 50%, not genuine per-instrument skill; the two furthest outliers
(SENSEX at 66.7%, SILVER at 39.0%) both have the smallest samples (21 and
59) of the eleven.

## Finding

**Both checks point the same way.** The directional bias call has no
measurable edge over random guessing (49.9% vs. an expected 50%), and the
confidence score layered on top of it doesn't correlate with which trades
actually work out (-0.0115). This is consistent with, and explains, every
prior finding in this series:

- Why the entry/SL/target formula's real performance was weak
  (`ENTRY_SL_TARGET_BACKTEST_REPORT.md`) despite arithmetically correct
  math -- you cannot manage risk into positive expectancy on a coin-flip
  entry using an option-buying structure (theta decay, spread, and the
  15%/35% floors all work against you by default; a genuine directional
  edge is what would need to overcome that drag).
- Why no combination of `sl_percent`/`min_target_percent`/
  `MAX_HOLD_MINUTES` crossed breakeven in the 18-combination grid search
  (`SL_TARGET_RETUNE_REPORT.md`) -- exit-parameter tuning cannot create an
  edge that doesn't exist at entry.

**This is not a coding bug.** `detect_bias()` and `generate_signal()`'s
confidence logic run exactly as documented; `AI_TRADING_INTELLIGENCE.md`
already flagged its own weights as "transparent, hand-weighted arithmetic,
not fitted or backtested models... not because they've been empirically
validated to be optimal" (line 196) -- this report is that empirical
validation, run for the first time, and the result is that the current
weighting scheme isn't adding real predictive value on this archive.

## What this report does NOT do

No code, weight, or threshold is changed here. Redesigning the bias/
confidence logic to find a real edge -- new features, different weights,
a fundamentally different signal source -- is a materially larger
undertaking than anything in this report series so far, and picking new
logic without its own rigorous backtest would repeat the exact mistake
this project's discipline exists to prevent. This is a diagnosis, not a
fix, and the decision on whether/how to pursue one belongs to the
operator.

## What's next (not started here)

- If a redesign is wanted, the natural next step is feature-by-feature:
  test each of the ~8 confidence bonuses in isolation (does volume
  dominance alone predict anything? does structural proximity alone?)
  rather than only the combined score -- one of them might carry real
  signal even though the aggregate doesn't.
- `target_delta_approx` and the OI-wall-based target/SL projection
  mechanics were not implicated by this report (they were already
  covered by the two prior reports) -- this report is specifically about
  the CE/PE direction decision upstream of them.
- Given a coin-flip entry, the honest headline number for the whole
  pipeline is closer to "these three reports, taken together, found no
  evidence of a tradeable edge in the current rule-based engine on this
  archive" -- worth stating plainly rather than only in pieces across
  three separate documents.
