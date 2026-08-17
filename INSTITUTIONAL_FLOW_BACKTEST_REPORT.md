# Institutional Buying/Selling Backtest Report

Backtests `agents/trading_intelligence/institutional_intelligence.py`'s
`institutional_flow_findings()` detector -- real, historical replay of
`oi_history.db`'s `cycles`/`strikes` archive (2026-07-13 to 2026-08-17, ~35
days) through the SAME detection logic the live system runs every cycle, per
`AI_TRADING_INTELLIGENCE.md`'s own long-standing "Future improvements" item.
Implementation: `agents/trading_intelligence/institutional_flow_backtest.py`.

## Scope

**Only `institutional_flow_findings()` was backtested.**
`gamma_trap_findings()` is excluded: it requires a real historical
`expiry_date`, and this repo has no historically-reconstructable
`expiry_date` anywhere (no `expiry_date` column in `cycles`/`strikes`/
`market_structure_snapshots`; the only expiry-resolution path,
`expiry_intelligence.get_nearest_expiry()`, requires a live broker session
reading *today's* instrument master, which never retains already-expired
series). This mirrors `structure_backtest.py`'s own precedent of explicitly
documenting a deliberate scope exclusion.

**Discrepancy since fixed (see Addendum below):** at the time of the original
run below, `institutional_flow_findings()`'s two `compute_volume_expansion()`
calls omitted `expansion_mult=`, so they ran against that function's own
default (`1.5`) rather than this module's documented
`INSTITUTIONAL_OI_EXPANSION_MULT` (`2.0`). The Results/Conclusion sections
below describe that original, since-superseded 1.5x behavior; the Addendum
has the 2.0x comparison and the fix decision.

## Methodology

- **Direction**: `oi_engine.net_oi_buildup_lean()` (this codebase's existing
  CE/PE buildup convention) -- CE Long Buildup / PE Short Buildup = BULLISH;
  CE Short Buildup / PE Long Buildup = BEARISH.
- **Outcome window**: 30 minutes (mirrors `backtest.MAX_HOLD_MINUTES`), never
  crossing into the next trading day.
- **Win/loss threshold**: `INSTITUTIONAL_OI_EXPANSION_MULT × sigma`, where
  `sigma` is the population stdev of cycle-to-cycle `underlying_ltp`
  differences over the trailing 20 same-day cycles -- a real, self-contained,
  price-only measure of each instrument's own typical short-term movement.
  WIN if the underlying moves past this threshold in the predicted direction
  before moving past it against; LOSS otherwise; PENDING if neither resolves
  within the window/day.
- **Dedup**: `institutional_flow_findings()` can re-fire on the same
  `(strike, side)` every ~7-15s while OI stays elevated -- treated as one
  event per cooldown window, not independent samples.
- **Minimum sample size**: 20 resolved (WIN/LOSS) outcomes -- below this,
  `win_rate` is honestly reported as not-yet-available, never a fabricated
  number.
- **Lookahead-bias fix**: `institutional_flow_findings()`'s own internal
  `data_access.recent_strike_history()` call (normally "live most-recent 10
  cycles," no date bound) is replaced for the duration of each symbol's replay
  with a strictly-historical, incrementally-fed equivalent -- verified by a
  dedicated regression test (`TestNoLookaheadLeakage`) proving the real
  function is never invoked during replay and is fully restored afterward.

## Results

11 symbols (`config.TI_WATCHED_SYMBOLS`), full archive range:

| Symbol | Sample | Wins | Losses | Pending | Excluded | Win rate | p-value vs 50% |
|---|---:|---:|---:|---:|---:|---:|---:|
| NIFTY | 17 | 8 | 9 | 0 | 25 | N/A (below min sample) | -- |
| BANKNIFTY | 38 | 21 | 17 | 5 | 91 | 55.26% | 0.627 |
| SENSEX | 32 | 14 | 18 | 1 | 10 | 43.75% | 0.597 |
| NATURALGAS | 7 | 3 | 4 | 0 | 4 | N/A (below min sample) | -- |
| NATGASMINI | 14 | 6 | 8 | 1 | 17 | N/A (below min sample) | -- |
| CRUDEOIL | 98 | 44 | 54 | 0 | 59 | 44.90% | 0.363 |
| CRUDEOILM | 110 | 63 | 47 | 0 | 126 | 57.27% | 0.152 |
| GOLD | 9 | 4 | 5 | 1 | 6 | N/A (below min sample) | -- |
| GOLDM | 32 | 14 | 18 | 2 | 22 | 43.75% | 0.597 |
| SILVER | 12 | 7 | 5 | 0 | 1 | N/A (below min sample) | -- |
| SILVERM | 120 | 63 | 57 | 2 | 7 | 52.50% | 0.648 |

`Excluded` = findings that fired before 20 same-day prior cycles existed
(threshold couldn't be honestly computed) -- tracked, never silently dropped.
`p-value` = exact two-sided binomial test against 50%, computed without
external dependencies (this venv has no scipy).

5 of 11 symbols (NIFTY, NATURALGAS, NATGASMINI, GOLD, SILVER) never reached
the minimum sample size of 20 resolved outcomes across the full ~35-day
archive -- an honest, real finding, not a forced number.

## Conclusion

**Do not graduate `institutional_flow_findings()` out of advisory-only.**
Of the 6 symbols with enough data to judge, every win rate is close to 50%
(43.75%-57.27%) and **none is statistically distinguishable from a coin flip**
(all p-values > 0.15, most > 0.35, against a two-sided binomial test). This is
a real, honest result -- not "the detector is broken," but "this backtest
found no evidence yet that it predicts direction better than chance." The
detector should keep its existing advisory/caveated framing.

## Caveats

- Real, recorded live paper-trade history (as opposed to this raw-archive
  replay) still has zero examples of this signal (only 5 trading days
  collected so far, Aug 10-14) -- this backtest is the only real evidence
  currently available, not yet corroborated by actual trade outcomes.
- ~35 days of archive, one market regime -- not a claim about how this
  detector performs across a full market cycle or under different volatility
  regimes.

## Addendum: 1.5x vs 2.0x comparison and the fix decision

Before deciding whether to fix the `expansion_mult` discrepancy noted above,
this backtest was re-run over the identical archive window with both
`compute_volume_expansion()` call sites forced to `expansion_mult=
INSTITUTIONAL_OI_EXPANSION_MULT` (2.0x), to see whether the documented,
stricter threshold actually predicts direction any better than what was
shipping.

| Symbol | Sample (1.5x -> 2.0x) | Win rate (1.5x -> 2.0x) | p-value (2.0x) |
|---|---|---|---:|
| NIFTY | 17 -> 10 | N/A -> N/A | -- |
| BANKNIFTY | 38 -> 17 | 55.26% -> N/A | -- |
| SENSEX | 32 -> 16 | 43.75% -> N/A | -- |
| NATURALGAS | 7 -> 1 | N/A -> N/A | -- |
| NATGASMINI | 14 -> 4 | N/A -> N/A | -- |
| CRUDEOIL | 98 -> 36 | 44.90% -> 52.78% | 0.868 |
| CRUDEOILM | 110 -> 51 | 57.27% -> 58.82% | 0.262 |
| GOLD | 9 -> 8 | N/A -> N/A | -- |
| GOLDM | 32 -> 17 | 43.75% -> N/A | -- |
| SILVER | 12 -> 8 | N/A -> N/A | -- |
| SILVERM | 120 -> 57 | 52.50% -> 43.86% | 0.427 |

The stricter 2.0x threshold roughly halves every symbol's sample size (fewer
strikes clear the higher bar) and pushes 3 more symbols (BANKNIFTY, SENSEX,
GOLDM) below the 20-sample floor entirely. Of the 3 symbols with enough data
to judge at 2.0x, the direction of change is mixed (CRUDEOIL and CRUDEOILM
improve slightly, SILVERM gets worse) and none is statistically
distinguishable from 50% either (p=0.868, 0.262, 0.427) -- the same "no
demonstrated edge" conclusion as the original 1.5x run, just on a smaller
sample.

**Decision: fixed to 2.0x anyway** (both `compute_volume_expansion()` call
sites in `institutional_flow_findings()` now pass
`expansion_mult=INSTITUTIONAL_OI_EXPANSION_MULT` explicitly). This backtest
found no evidence that either threshold predicts direction better than the
other, so there was no data-driven reason to keep the undocumented 1.5x
default that reached production only because two call sites omitted a
keyword argument. Matching the code to what the module's own docstring and
constant have said all along is the honest default in the absence of a
reason to prefer the accidental behavior. The detector remains
advisory-only regardless -- this fix does not change that conclusion, only
which threshold the (still-unproven) heuristic actually uses.
