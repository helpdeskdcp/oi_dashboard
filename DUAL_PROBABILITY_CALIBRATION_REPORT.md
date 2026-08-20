# Dual-Probability Calibration — Findings Report

**Status: shadow-only infrastructure, deliberately NOT wired into `signal_graph.py`. `dual_probability_store.py` deliberately not built.**

## What this was

PR #27 built a two-event (`TARGET_EVENT` / `STOP_SAFETY_EVENT`) calibrated
probability model as isolated, shadow-only infrastructure: `dual_probability_labels.py`
(walk-forward event labeling), `dual_probability_features.py` (7 evidence
groups: trend/momentum/structure/OI/regime/volume/MTF), `dual_probability_calibration.py`
(logistic + isotonic recalibration, numpy-only), and `dual_probability_backtest.py`
(CLI + report builder). 52 unit tests, all passing. Zero production files
modified, nothing called from any live code path.

The eventual goal — never reached — was a live/shadow node that could
honestly say "this signal has an X% chance of hitting target before stop,"
calibrated well enough that "95% means 95%," gating high-confidence entries.

## Why it's not wired in

Two rounds of real validation, both against genuine historical data (never
synthetic), found the model isn't there yet:

**Round 1 (arbitrary bars, every bar both directions):** stop-safety model
showed real, reproducible skill (holdout Brier ~0.12–0.16 vs 0.25 random).
Target model was weaker (holdout Brier ~0.19–0.25, one direction at
random-guessing level). Critically: on this dataset, predictions never
reached the 85%+ confidence tier the eventual system would need to gate on
— there was nothing to validate the "95% means 95%" claim against at all.

**Round 2 (real oi_engine-proposed signals, 5 symbols, 2026-07-13 to
2026-08-18, deduplicated for correlated re-fires):**
- **Target-probability model: never validated at any horizon tested.**
  60–85% of real signals never resolve (neither target nor stop touched)
  within `MAX_HOLD_MINUTES=30`. Even after decoupling the dedup cooldown
  from the horizon and sweeping 10/20/30/60 bars, every symbol's
  resolved-outcome sample size stayed below the 30-sample floor. The trend
  across horizons was clean and consistent (e.g. CRUDEOILM short:
  15/21/24/28 resolved samples at h=10/20/30/60), suggesting a wider
  horizon (90/120 bars) is a well-justified *next experiment*, not evidence
  the model already works.
- **Stop-safety model: valid in 3 of 8 symbol/direction cases.** Where
  valid, genuinely good (NATURALGAS short holdout Brier=0.046, NATGASMINI
  short holdout Brier=0.007, both far below the 0.25 baseline). But NOT
  uniform: CRUDEOILM short's calibration-set Brier looked reasonable
  (0.139) while its holdout Brier was 0.300 — *worse* than random, a real
  overfitting/generalization failure, reported honestly rather than hidden.
- Volume and MTF feature gaps (the two groups that were `None` in Round 1)
  were closed for real in a follow-up commit (Money Flow Index from real
  volume where it exists — zero for NSE index symbols, honestly; a
  point-in-time-safe 3m→15m multi-timeframe agreement score) — this closed
  a data gap but did not change the sample-size/overfitting findings above.

## The actual blocker

Not a code problem — an evidence problem. Wiring a shadow node into
`signal_graph.py` (which already feeds the live scheduler cycle, unlike
this model's own isolated modules) on a target-probability model that has
never validated, or a stop-safety model that overfits in a quarter of
tested cases, would put an unvalidated number in front of real users
without the honest "insufficient sample" gate this project's own
convention requires (see `ai_trading_engine.evaluate()`'s own
`probability=None` behavior below 5 real trades per bucket — the same
discipline should apply here, and doesn't yet have enough data to apply
usefully).

## What would unblock this

Either of:
1. A wider resolution horizon (90/120+ bars) that gets the target model's
   resolved-sample counts above 30 for most symbols, confirmed by
   re-running `dual_probability_backtest.py`'s existing sweep.
2. Enough real accumulated production history that the stop-safety model's
   overfitting cases (CRUDEOILM short, and any others found on a wider
   symbol sweep) can be distinguished from genuine skill with a proper
   holdout, not just flagged as inconsistent.

Neither is a coding task — both require more real market data to
accumulate or a parameter sweep to re-run against a larger archive.
Re-run `python3 -m agents.trading_intelligence.dual_probability_backtest`
against the live archive periodically; this report should be updated (not
silently superseded) once either condition is met.
