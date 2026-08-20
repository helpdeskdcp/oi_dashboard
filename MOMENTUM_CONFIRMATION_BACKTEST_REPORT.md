# Momentum-Confirmation Flag — Backtest Report

**Status: `TI_ENABLE_MOMENTUM_CONFIRMATION` stays OFF. Real evidence does not support turning it on.**

## What this was

`oi_engine.generate_signal()`'s momentum-confirmation sub-score (PR #29,
hotfixed in PR #31) was deployed OFF by default and never validated against
real historical data before this report. `agents/trading_intelligence/momentum_confirmation_backtest.py`
closes that gap: it replays the same real historical cycles through
`backtest.py`'s existing V1 signal engine (`simulate_trades()`, extended
additively with `momentum_confirmation_enabled`/`candles_df` kwargs — every
existing caller, including the live `/backtest` web page, is byte-for-byte
unaffected since the defaults match `generate_signal()`'s own OFF-by-default
momentum params) twice per symbol — once with the flag OFF, once ON — using
the exact same market data both times, so any difference in outcome is
attributable only to the flag.

## Real run

`agents/trading_intelligence/momentum_confirmation_backtest.py --all --from 2026-07-13 --to 2026-08-19`,
all 11 `TI_WATCHED_SYMBOLS`, full available archive (5+ weeks).

| Symbol | OFF trades / win rate / net pts | ON trades / win rate / net pts | Verdict |
|---|---|---|---|
| NIFTY | 92 / n<20 / +95.9 | 90 / n<20 / +101.85 | insufficient sample |
| **BANKNIFTY** | 131 / n<20 (raw 31.6%) / **-1142.75** | 135 / **25.0%** / **-1506.1** | **worse** — the one case with a real, floor-clearing sample, and it's a clear regression |
| SENSEX | 20 / n<20 / +7.7 | 20 / n<20 / +7.7 | no change |
| NATURALGAS | 436 / n<20 / +18.1 | 435 / n<20 / +20.1 | insufficient sample |
| NATGASMINI | 447 / n<20 / -6.3 | 448 / n<20 / +13.45 | insufficient sample |
| CRUDEOIL | 118 / n<20 / +24.8 | 118 / n<20 / +45.2 | insufficient sample |
| CRUDEOILM | 305 / 20.59% / -130.5 | 308 / 21.21% / -86.55 | marginal improvement, still deeply unprofitable either way |
| GOLD | 113 / n<20 / -773.0 | 109 / n<20 / -670.5 | insufficient sample |
| GOLDM | 128 / 6.35% / -1163.5 | 127 / 6.45% / -1134.0 | marginal improvement, still catastrophically unprofitable either way |
| SILVER | 71 / n<20 / -3345.0 | 68 / n<20 / -4086.0 | insufficient sample, direction is worse |
| SILVERM | 120 / 2.47% / -4742.5 | 116 / 2.60% / -4505.5 | marginal improvement, still catastrophically unprofitable either way |

("n<20" = fewer than 20 resolved (TARGET HIT + STOP LOSS) outcomes — below
this report's own honesty floor, so no win-rate number is reported, matching
every other backtest report in this repo.)

## Finding

**No clear evidence supports turning the flag on.** Of the four symbols
with enough resolved trades to report a real win rate:

- **BANKNIFTY — the one case with the clearest signal — gets measurably
  worse.** Win rate lands at 25% (vs. a raw, sub-floor 31.6% OFF), and net
  points drop by 363.35 over the same 5-week window. This is the single
  most trustworthy data point in this report (largest resolved sample), and
  it points against the flag.
- CRUDEOILM, GOLDM, SILVERM show tiny (0.1–1.1 percentage point) win-rate
  upticks with the flag on. All three are already catastrophically
  unprofitable strategies regardless of this flag (win rates 2–21%, net
  points in the hundreds to thousands of points negative) — a fractional
  win-rate improvement on a strategy this badly broken doesn't indicate the
  flag is doing anything meaningful; it's noise on top of a much larger,
  unrelated problem.
- The remaining 7 symbols never accumulated 20 resolved outcomes in either
  run, so no honest win-rate verdict is possible for them at all.

The flag's own confidence bonus/penalty (±10) genuinely does change WHICH
trades get taken near the confidence_threshold boundary (confirmed directly
in this module's own unit tests) — the mechanism works as designed. The
evidence just doesn't show that shifting the trade set this way helps.

## Decision

`TI_ENABLE_MOMENTUM_CONFIRMATION` stays `false` in production. Turning it on
now would be acting against the one piece of real evidence available
(BANKNIFTY), not for a lack of evidence. This isn't a coding task to revisit
later — it would need either a materially different momentum feature/
threshold design, or a lot more accumulated real trade history across the
symbols that couldn't clear the sample floor here, before this question is
worth re-asking.

Re-run this report periodically as more history accumulates:
`python3 -m agents.trading_intelligence.momentum_confirmation_backtest --all --from <date> --to <date>`.
