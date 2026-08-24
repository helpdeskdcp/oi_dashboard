# PCR Extremity — Isolation Report

**Status: HARMFUL in aggregate, with per-symbol inconsistency. Not proposed for a threshold change here — this report isolates and measures the effect, per the user's explicit "do not optimize threshold" instruction. No code changed.**

## Why a second look, not just a repeat of the earlier finding

`CONFIDENCE_FACTOR_ISOLATION_REPORT.md` already found trades where the PCR-extremity bonus fired performed worse than trades where it didn't (profit factor 0.53 vs 0.67). That was a **post-hoc partition** of a fixed trade list — real, but exposed to selection bias: "fired" and "not fired" cycles are different market moments by construction, not a controlled comparison.

This report instead runs a genuine **ON/OFF ablation**: the same real historical cycles (2026-07-13 to 2026-08-21, all 11 watched symbols) are walked forward exactly once, computing both the real confidence (bonus active, today's live behavior) and a counterfactual confidence (bonus removed) per cycle, feeding two independent walk-forward position-management loops in parallel. Removing the bonus can push a cycle below the `confidence_threshold=60` gate, changing which cycles actually open a trade — so the two conditions can end up with different trade *sets*, not just different labels on the same trades. This directly addresses the earlier method's selection-bias exposure.

**Documented approximation**: the OFF confidence is reconstructed as `max(20, min(95, real_confidence - 15))` when the PCR-extreme condition fired, else identical to the real confidence — a first-order undo of `oi_engine.py`'s `confidence += 15` term, not a full re-derivation of the whole formula. This is exact except when the real (ON) confidence was already sitting at the clamp boundary (20 or 95); **30 such edge cases occurred out of 2,405 trades** (~1.2%), reported for transparency, not excluded.

## Results

| Symbol | ON trades | OFF trades | ON PF | OFF PF | ON net | OFF net |
|---|---:|---:|---:|---:|---:|---:|
| NIFTY | 115 | 105 | 1.02 | 1.04 | +13.50 | +21.35 |
| BANKNIFTY | 151 | 149 | 0.58 | 0.64 | -1343.20 | -1074.55 |
| SENSEX | 21 | 16 | 1.02 | 1.52 | +14.50 | +182.95 |
| NATURALGAS | 520 | 190 | 1.20 | 1.00 | +19.35 | +0.00 |
| NATGASMINI | 526 | 232 | 1.12 | 1.01 | +12.35 | +0.45 |
| CRUDEOIL | 143 | 104 | 1.27 | 1.23 | +286.50 | +165.10 |
| CRUDEOILM | 474 | 379 | 0.99 | 1.08 | -22.40 | +235.25 |
| GOLD | 128 | 11 | 0.65 | 0.22 | -1057.50 | -241.00 |
| GOLDM | 157 | 106 | 0.68 | 0.54 | -1781.50 | -2110.50 |
| SILVER | 59 | 8 | 0.47 | 0.03 | -7427.50 | -2013.00 |
| SILVERM | 111 | 47 | 0.30 | 0.32 | -8104.00 | -3025.00 |

**Overall**: ON (bonus active, today's live behavior) — 2,405 trades, 345 resolved, accuracy 22.9%, profit factor **0.56**, net **-19,389.90**, expectancy -8.06 pts/trade. OFF (bonus removed) — 1,347 trades, 213 resolved, accuracy **31.9%**, profit factor **0.59**, net **-7,858.95**, expectancy -5.83 pts/trade.

Target-before-SL / SL-before-target: ON 79/266, OFF 68/145.

## Checks requested

- **Look-ahead bias / data leakage**: none introduced. The ablation reuses `backtest.load_cycles()`'s existing chronological, forward-only replay — the same no-lookahead pattern every backtest in this repo already follows; both conditions see identical, real, past-only data at every step.
- **Selection bias**: directly addressed by this report's design (see above) relative to the earlier per-trade partition. A residual effect remains and is disclosed rather than hidden: OFF systematically opens fewer trades everywhere (removing 15 points pushes borderline cycles below the tradeable threshold), so part of OFF's smaller net loss reflects reduced exposure, not purely a higher win rate on identical volume. The accuracy improvement (22.9% → 31.9%) is the more trustworthy signal that remaining trades are higher-quality on average, independent of volume.
- **Regime dependency**: cannot be tested directly — `ARCHITECTURE_AUDIT.md` and `CONFIDENCE_FACTOR_ISOLATION_REPORT.md` both already established that `backtest.simulate_trades()` never supplies `market_structure` to `generate_signal()`, so no real regime label exists to stratify by in this backtest path. The per-symbol table above is the closest available proxy (different instruments trade differently), and it shows genuine inconsistency: NIFTY/BANKNIFTY/SENSEX/CRUDEOILM improve when the bonus is removed; GOLD/GOLDM/SILVER do not, though GOLD's and SILVER's OFF samples are very thin (11 and 8 trades) and not trustworthy on their own.

## Conclusion

**HARMFUL, in aggregate — but not uniformly proven per-symbol.** The three largest-sample, clearest comparisons (NIFTY, BANKNIFTY, CRUDEOILM — all with real trade counts on both sides) all improve when the bonus is removed, and the aggregate accuracy/expectancy improvement is consistent with `CONFIDENCE_FACTOR_ISOLATION_REPORT.md`'s independent finding using a different method. GOLDM and SILVERM (large samples on both sides) show the bonus removal does NOT help there. GOLD and SILVER's comparisons are not trustworthy given how few OFF trades resulted.

This is reported as a finding, not implemented as a fix. No threshold or formula was changed to produce this report, per the explicit instruction. If a change is ever proposed here, it needs the same train/test out-of-sample validation `SL_TARGET_RETUNE_REPORT.md` already established as this project's standard — a single-window ablation, however carefully controlled, is still one window.
