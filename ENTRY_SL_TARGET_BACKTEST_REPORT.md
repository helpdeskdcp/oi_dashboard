# Entry / Stop-Loss / Target Formula — Backtest Report

**Status: the formula's arithmetic is structurally correct (0 violations across 2,405 real trades). Its real-world performance, at the current parameters, is not — the full-archive aggregate is net unprofitable, and only 1 of 6 symbols with a trustworthy sample is even close to breakeven. No parameter change is made in this report — see "What this report does NOT do" below.**

## What this was

`oi_engine.generate_signal()`'s entry/SL/target formula decides every real
(paper) trade's actual price levels for every watched symbol. It had never
been backtested against real historical data or unit-tested at all — see
`test_oi_engine_momentum_confirmation.py`'s own docstring, written when that
file was added: *"not a full backfill of generate_signal()'s existing
(untested) confidence logic — that's a separate, larger task."* This report,
together with the new `test_oi_engine_signal_math.py`, is that task.

**The formula** (unchanged by this report, documented here for reference):
- `entry_price` = the ATM strike's own live CE/PE LTP.
- `target_price = entry + max(delta_used × |OI-wall strike − ATM|, entry × 15%)`
  — projects the underlying's likely move to the strongest OI wall on the
  profit side onto the option premium via delta, floored at 15% of entry.
- `sl_price = max(structural_invalidation_sl, entry × (1 − 35%), ₹0.05)` —
  the tighter of a structural stop (projected the same way onto the
  *nearest* opposing-side OI wall) and a flat 35%-of-entry floor.
- `delta_used` = a flat 0.55 approximation unless real NSE IV is available
  for a Black-Scholes delta that cycle.

## Real run

`backtest.simulate_trades()` (the same function `backtest.py`'s own CLI and
the live `/backtest` web page use — this is the exact formula the live
dashboard runs, per `oi_engine.py`'s own module docstring guarantee that
live and backtest never diverge), all 11 `TI_WATCHED_SYMBOLS`, full
available archive (`2026-07-13` to `2026-08-24`, ~6 weeks), default
parameters (`persistence_cycles=2, cooldown_minutes=10,
confidence_threshold=60` — matching `SIGNAL_CONFIDENCE_THRESHOLD` and
`generate_signal()`'s own defaults).

| Symbol | Trades | Resolved | Accuracy (excl. time exits) | Profit Factor | Net Points |
|---|---:|---:|---:|---:|---:|
| NIFTY | 115 | 24 | 62.5% | 1.02 | +13.50 |
| **BANKNIFTY** | 151 | 23 | 26.1% | 0.58 | **-1343.20** |
| SENSEX | 21 | 5 | n<20 (raw 60.0%) | 1.02 | +14.50 |
| NATURALGAS | 520 | 18 | n<20 (raw 88.9%) | 1.20 | +19.35 |
| NATGASMINI | 526 | 14 | n<20 (raw 92.9%) | 1.12 | +12.35 |
| CRUDEOIL | 143 | 15 | n<20 (raw 33.3%) | 1.27 | +286.50 |
| CRUDEOILM | 474 | 49 | 22.4% | 0.99 | -22.40 |
| **GOLD** | 128 | 24 | 8.3% | 0.65 | **-1057.50** |
| **GOLDM** | 157 | 82 | 6.1% | 0.68 | **-1781.50** |
| SILVER | 59 | 14 | n<20 (raw 7.1%) | 0.47 | -7427.50 |
| **SILVERM** | 111 | 77 | 2.6% | 0.30 | **-8104.00** |

("n<20" = fewer than 20 resolved (TARGET HIT + STOP LOSS) outcomes — below
this repo's own honesty floor for reporting a trustworthy accuracy figure,
matching `MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md`'s exact convention. The
raw figure is still shown, not hidden, just not treated as reliable.)

**Overall (2,405 trades, 345 resolved — well above the floor):**
Accuracy 22.9% · Win rate (all trades, time-exits included) 42.4% · Profit
factor **0.56** · Net points **-19,389.90** · Expectancy **-8.06 pts/trade**
· Max drawdown 19,750.50 pts · Sharpe (per-trade, not annualized) -0.037.

## Finding

**The formula's arithmetic is correct.** Across every one of the 2,405 real
trades this backtest generated, `sl_price < entry_price`,
`target_price > entry_price`, and `entry_price > 0` held without a single
exception — the structural invariants `test_oi_engine_signal_math.py` now
locks in as tests. There is no coding bug here.

**Its real-world performance, at today's parameters, is not good.** Of the
6 symbols with a resolved sample large enough to trust (≥20):

- Only **NIFTY** is close to breakeven (profit factor 1.02, accuracy 62.5%).
- **BANKNIFTY, GOLD, GOLDM, SILVERM are clearly losing** — profit factors
  0.30–0.68, net points -1,057 to -8,104 over the same 6-week window,
  accuracy as low as 2.6%.
- **CRUDEOILM** is roughly flat (profit factor 0.99, net -22.40).

This is not a new problem introduced by anything in this backtest — it's a
generalization of something already partially visible in
`MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md`'s own baseline ("OFF") column,
which already named BANKNIFTY, GOLDM, and SILVERM as "catastrophically
unprofitable" while investigating a *different* question (the momentum
flag). This report makes that finding explicit and confirms it's about the
core entry/SL/target formula itself, not the momentum add-on, and extends
it with a proper accuracy floor and the full current symbol set.

The dominant outcome by far is **TIME EXIT** (2,060 of 2,405 trades, 85.7%)
— most trades never reach either target or stop within the 30-minute
(`MAX_HOLD_MINUTES`) hold window at all. Whatever is driving the losing
symbols' poor profit factor, it is happening mostly within that minority of
trades that DO resolve, not from time-exits themselves (time-exits are
scored at whatever premium the option happened to be at when the 30-minute
window ran out, which can be either side of entry).

## What this report does NOT do

This report does not change `target_delta_approx`, `sl_percent`,
`min_target_percent`, or `MAX_HOLD_MINUTES`. Picking new numbers without
backtesting THEM would repeat the exact mistake this project's own
established discipline exists to avoid (see every prior report in this
repo — momentum confirmation, dual-probability calibration, institutional
flow). A parameter change here needs its own before/after backtest
comparison, the same rigor this report itself just applied. This is a
findings report, not a fix — the decision on whether/how to retune belongs
to the operator, not an unattended parameter guess.

## What's new in this pass

- `test_oi_engine_signal_math.py` (new, 26 tests): locks in the formula's
  actual arithmetic — entry price sourcing, target's delta-projection-vs-
  percent-floor branches, SL's structural-vs-floor-vs-5-paise-minimum
  branches, nearest-wall (not highest-OI-wall) invalidation-strike
  selection for both CE and PE, the `action` field's BUY-only contract,
  and the two structural invariants (`sl < entry`, `target > entry`) this
  backtest run also verified held for all 2,405 real trades.
