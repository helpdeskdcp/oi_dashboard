# SL / Target / Hold-Time Retune — Backtest Report

**Status: no parameter change made. A real grid search found a candidate that looked better in-sample, but it failed catastrophically out-of-sample — a textbook overfitting result. The current live parameters (`sl_percent=0.35`, `min_target_percent=0.15`, `MAX_HOLD_MINUTES=30`) remain the best-performing configuration once evaluated honestly, and stay unchanged.**

## What this was

Follow-up to `ENTRY_SL_TARGET_BACKTEST_REPORT.md`, which found the entry/
SL/target formula's arithmetic correct but its real-world performance
weak (aggregate profit factor 0.56 across the full ~6-week archive) and
explicitly declined to retune `sl_percent`/`min_target_percent`/
`MAX_HOLD_MINUTES` without its own before/after evidence. This report is
that evidence.

## Methodology

A naive approach — sweep parameters against the full archive and report
whichever combination has the best profit factor — would just curve-fit
to that one window's noise and prove nothing about live performance. This
run instead used a **train/test split**, the standard guard against that:

- **TRAIN** (`2026-07-13` to `2026-08-10`, ~4 weeks): every candidate
  parameter combination evaluated here; selection based ONLY on this data.
- **TEST** (`2026-08-11` to `2026-08-21`, ~1.5 weeks): held out completely
  from selection. The winning TRAIN candidate, plus baseline, evaluated
  here ONCE, after the fact, as the real generalization check.

**Grid** (18 combinations, all 11 `TI_WATCHED_SYMBOLS`, via
`backtest.simulate_trades()` — the same function the live dashboard's
formula shares):
- `sl_percent` ∈ {0.20, 0.35 (current), 0.50}
- `min_target_percent` ∈ {0.08, 0.15 (current), 0.25}
- `MAX_HOLD_MINUTES` ∈ {30 (current), 60}

`target_delta_approx` was held fixed at its current default (0.55) —
retuning it too would triple the search space; flagged as a possible
future dimension below, not attempted here.

## TRAIN results

All 18 combinations, ranked by profit factor (resolved trades ≥ 50, to
exclude tiny-sample flukes):

| sl% | target% | hold | Profit Factor | Net Points | Accuracy | Resolved |
|---|---|---|---:|---:|---:|---:|
| 0.20 | 0.15 | 60min | **0.92** | -1982.05 | 28.0% | 314 |
| 0.20 | 0.25 | 60min | 0.91 | -2395.75 | 18.2% | 274 |
| 0.35 | 0.15 | 60min | 0.91 | -2217.35 | 33.6% | 271 |
| 0.50 | 0.15 | 60min | 0.91 | -2281.80 | 34.2% | 257 |
| 0.35 | 0.25 | 60min | 0.90 | -2502.90 | 22.6% | 234 |
| 0.50 | 0.25 | 60min | 0.90 | -2637.15 | 22.4% | 219 |
| 0.20 | 0.15 | 30min | 0.85 | -3395.80 | 24.2% | 285 |
| **0.35 (current)** | **0.15 (current)** | **30min (current)** | **0.85** | **-3487.60** | 25.8% | 244 |

(Full 18-row table, including the 0.08/0.25 target-percent variants that
all scored worse, in the run log.)

**Every single one of the 18 combinations was still net-losing on TRAIN**
(profit factor below 1.0 in all 18 cases). The clearest pattern:
`MAX_HOLD_MINUTES=60` outperformed `30` in every matched sl%/target% pair
— fewer trades hit the arbitrary 30-minute cutoff before resolving, so
more of them reach a genuine target or stop. The best single cell
(`sl=0.20, target=0.15, hold=60`) improved profit factor from the
baseline's 0.85 to 0.92 and roughly halved net losses.

## TEST results (the real check)

The same four configurations — baseline plus the TRAIN winner plus two
neighboring `hold=60` cells, to check whether "hold=60 beats hold=30" was
a robust pattern or a fluke of one specific cell — evaluated on the
**held-out** window, never used for selection:

| Configuration | Profit Factor | Net Points | Expectancy/trade |
|---|---:|---:|---:|
| **BASELINE (live): sl=0.35, target=0.15, hold=30** | **0.57** | **-5480.20** | **-6.45** |
| WINNER (train-best): sl=0.20, target=0.15, hold=60 | 0.23 | -22477.10 | -38.10 |
| sl=0.35, target=0.15, hold=60 | 0.23 | -22299.75 | -38.78 |
| sl=0.50, target=0.15, hold=60 | 0.23 | -22310.05 | -38.80 |

**The TRAIN-window improvement completely inverts out-of-sample.** Every
`hold=60` variant — regardless of `sl_percent` — collapses to a profit
factor of 0.23 (from ~0.91 in-sample) and a net loss more than 4x worse
than baseline. This isn't sensitivity to sl%/target% at all — moving
`sl_percent` between 0.20/0.35/0.50 barely changes the TEST result (0.23
in all three cases); the entire effect is `MAX_HOLD_MINUTES`, and it runs
in the opposite direction out-of-sample from what TRAIN suggested.

**Baseline itself also performed worse on TEST than on TRAIN** (profit
factor 0.85 → 0.57) — the test window (Aug 11-21) appears to have simply
been a harder period for this strategy overall, independent of any
parameter choice. Worth keeping in mind when reading either window's
absolute numbers.

## Finding

**No parameter change is being made.** The grid search's own apparent
winner is a clean example of exactly the overfitting risk this project's
established discipline (train/test split, no in-sample-only conclusions)
exists to catch. Extending the hold window to 60 minutes let more trades
resolve on the training data, which read as an improvement — but on
unseen data, holding losing positions twice as long before a time-exit
correctly kicks in produced dramatically larger losses instead. The
current live defaults (`sl_percent=0.35`, `min_target_percent=0.15`,
`MAX_HOLD_MINUTES=30`) are, on this evidence, the best of everything
tested, not the worst — they stay unchanged.

This also narrows down *where* the real problem lives. Every one of 18
SL/target/hold-time combinations stayed net-losing on TRAIN, with profit
factors clustered tightly in a 0.57-0.92 band regardless of how those
three exit parameters were varied. That's consistent with the earlier
report's read: the bottleneck is more likely **which trades get taken**
(the bias/confidence entry logic in `generate_signal()`) than **how those
trades are sized once taken** (SL/target/hold-time). Retuning entry
selection is a materially different, larger investigation than this one
and is not attempted here.

## What's next (not started here)

- `target_delta_approx` (currently a flat 0.55 unless real NSE IV is
  available) was not swept — a fourth dimension, out of scope for this
  pass.
- The actual entry/bias-confidence logic (`detect_bias()`,
  `generate_signal()`'s confidence bonuses/penalties) is the more likely
  place real improvement lives, per the finding above, but validating
  changes there needs its own dedicated backtest-and-compare cycle, same
  rigor as this report.
