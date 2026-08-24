# Failure Taxonomy

Each code below is classified as one of: **ALREADY DETECTABLE TODAY** (cites the exact existing check), **PROPOSED — STRAIGHTFORWARD** (buildable from existing primitives, not yet built), or **PROPOSED — REQUIRES BACKTEST** (a hypothesis with no supporting evidence yet). No code changed to produce this document; `failure_gate.py` (cited below) is real, tested code that already exists on disk but is **uncommitted** — not yet part of the canonical path.

| Code | Status | Evidence / Citation | Notes |
|---|---|---|---|
| `DATA_INVALID` | PROPOSED — STRAIGHTFORWARD | `market_data.get_snapshot()`'s `snapshot.available`/`.reason` fields already exist and degrade honestly on missing data | No module currently maps this into a named failure code; the honest-degradation behavior itself already exists |
| `DATA_STALE` | PROPOSED — REQUIRES BACKTEST | No staleness/timestamp-age check found anywhere in the canonical path | Would need a defensible staleness threshold — not established by any evidence yet |
| `STRUCTURE_CONFLICT` | ALREADY DETECTABLE TODAY | `failure_gate.py`'s `check_major_level_proximity()` (uncommitted worktree) | Known limitation documented in that file's own docstring: undirected nearest-level search, can miss a hostile level behind a nearer friendly one |
| `OI_CONFLICT` | PROPOSED — STRAIGHTFORWARD | Dual-source Angel/NSE disagreement logic already exists inside `oi_engine.generate_signal()` as a confidence adjustment (not exposed as a discrete check) | `failure_gate.py`'s own docstring flags this exact gap: extracting it cleanly needs `generate_signal()` refactored to expose it as a separate field first |
| `FLOW_CONFLICT` | PROPOSED — REQUIRES BACKTEST | No distinct "flow" concept beyond OI exists in this repo per `ARCHITECTURE_AUDIT.md` | Would need its own definition before it's even a hypothesis |
| `CROWDING_RISK` | PROPOSED — REQUIRES BACKTEST | `ARCHITECTURE_AUDIT.md` §L: confirmed zero existing implementation anywhere in the repo | Explicitly flagged as a hypothesis, not to be built blind |
| `TRAP_RISK` | PROPOSED — REQUIRES BACKTEST | Same as above | Same |
| `BREAKOUT_FAILURE` | PROPOSED — STRAIGHTFORWARD | `regime_profile.classify_market_regime()`'s `_breakout_confirmation()` already checks confirmed vs. unconfirmed breakout, feeding `TRADEABILITY_WAIT`/`NO_TRADE` | `failure_gate.py`'s `check_regime()` already reuses this wholesale as a PASS/FAIL check |
| `RR_INVALID` | ALREADY DETECTABLE TODAY | `failure_gate.py`'s `check_reward_risk()` (uncommitted worktree), floor `MIN_RISK_REWARD = 1.0` | Neutral floor, not data-fitted |
| `TARGET_BLOCKED` | PROPOSED — STRAIGHTFORWARD | `check_major_level_proximity()` checks proximity to entry, not obstruction between entry and target | Distinct check from `STRUCTURE_CONFLICT` above — not yet built |
| `SL_INVALID` | ALREADY DETECTABLE TODAY | `oi_engine.generate_signal()`'s own SL formula structurally guarantees `sl_price < entry_price` (verified across 2,405 real trades, `ENTRY_SL_TARGET_BACKTEST_REPORT.md`); `failure_gate.check_reward_risk()` also fails closed if that invariant is ever broken | Belt-and-suspenders, not a gap |
| `PROBABILITY_UNCALIBRATED` | ALREADY DETECTABLE TODAY (as a finding, not a live-gating check) | `ENTRY_BIAS_SELECTION_REPORT.md`/`CONFIDENCE_FACTOR_ISOLATION_REPORT.md`/`PROBABILITY_CALIBRATION_AUDIT.md`: confidence-outcome correlation ≈ 0, bucket calibration off by ~35-55 percentage points | Documented fact, not yet wired as a gate that blocks a specific trade |
| `EV_NEGATIVE` | PROPOSED — REQUIRES BACKTEST | No EV computation exists anywhere (see `EXPECTED_VALUE_GATE_DESIGN.md`) | Blocked on `PROBABILITY_UNCALIBRATED` being resolved first |
| `DUPLICATE_SIGNAL` | PROPOSED — STRAIGHTFORWARD | `evaluate()`'s existing `if open_trades: return HOLD` guard already prevents a second trade while one is open for the same symbol | Covers the common case; see `PAPER_TRADING_DUPLICATION_AUDIT.md` for the edge case (concurrent scheduler + manual trigger) |
| `POSITION_CONFLICT` | ALREADY DETECTABLE TODAY | Same `open_trades` guard as above | |
| `SIGNAL_EXPIRED` | ALREADY DETECTABLE TODAY | `_check_open_trade_exit()`'s expiry-rollover detection (PR #42) | Detects and closes on rollover; doesn't prevent a *new* signal from being generated against a stale snapshot, a narrower gap |

## Summary

7 of 16 codes are already detectable today (mostly via the uncommitted `failure_gate.py` or existing `evaluate()`/`generate_signal()` logic); 5 are straightforward extensions of existing primitives; 4 (`DATA_STALE`, `FLOW_CONFLICT`, `CROWDING_RISK`, `TRAP_RISK`) are genuine hypotheses requiring backtest evidence before any implementation.
