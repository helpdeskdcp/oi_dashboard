# Canonical Signal Decision Architecture

**Status: documentation only. No code, engine, threshold, or production behavior changed by this PR. Ten documents (this one plus nine others, listed at the bottom) were produced in response to a detailed follow-up brief after the Phase 1 architecture audit (`ARCHITECTURE_AUDIT.md`, `ENGINE_REGISTRY.md`, PR #51). This document is the synthesis; the others hold the depth. Per the brief's own explicit instruction, implementation is not attempted here — this is the investigation and design phase only, to be separately approved before anything is built.**

## Why this exists

`ARCHITECTURE_AUDIT.md` found the real architectural problem isn't a lack of engines — it's decision integrity: `generate_signal()`'s confidence score is one additive point pile, and this session went on to prove (`CONFIDENCE_FACTOR_ISOLATION_REPORT.md`, `PCR_EXTREMITY_ISOLATION_REPORT.md`, `PROBABILITY_CALIBRATION_AUDIT.md`) that its most influential component runs backwards and its "probability" isn't calibrated. **Confidence is not authority** — this document and its companions design what would have to be true for a real failure-first veto architecture to exist, without building it yet.

## Phase 1 — Repository decision trace: `ai_trading_engine.evaluate()`

Traced end to end, real file:line citations, re-verified this pass (not just cited from memory):

| Step | Location | Input | Output | Veto capable? | Confidence-only? |
|---|---|---|---|---|---|
| 1. Snapshot fetch | `market_data.get_snapshot()` (`ai_trading_engine.py:523`) | symbol, expiry_date | `snapshot` (strikes, atm, pcr, underlying_ltp, `.available`) | **Yes** — `if not snapshot.available: return NO_TRADE` | No |
| 2. Candles + trend | `data_access.load_candles()`, `_price_trend_pct()` (528-529) | symbol | `price_trend_pct` | No | Feeds bias only |
| 3. Market structure | `data_access.latest_market_structure()` (530) | symbol | `market_structure` dict or `None` | No | Feeds bias/regime downstream |
| 4. Bias detection | `oi_engine.detect_bias()` (532-533) | rows, atm, pcr, price_trend_pct, underlying, market_structure | `market_bias`, `bias_note` | Indirect — NEUTRAL/RANGE bias later causes NO_TRADE inside `generate_signal()` | No |
| 5. Expiry context | `expiry_intelligence.compute_scalping_metrics()` (541-544) | rows, underlying, days_to_expiry, atm | `expiry_context` dict | No | No |
| 6. **Open-position guard** | `ti_store.list_open_trades()` (546) | symbol | existing open trade or none | **Yes** — the de facto single-open-position-per-symbol idempotency guard, see `PAPER_TRADING_DUPLICATION_AUDIT.md` for its real limit | No |
| 7. Data-completeness guard | line 579 | atm, pcr, rows | — | **Yes** — `NO_TRADE` if any missing | No |
| 8. OI walls | `oi_engine.oi_walls()` (582) | rows | `support`, `resistance` (top-3 by OI) | No | No |
| 9. **Core signal** | `oi_engine.generate_signal()` (583-586) | rows, atm, market_bias, bias_note, pcr, support, resistance, underlying, expiry_date, market_structure | action, direction, strike, entry_price, target_price, sl_price, confidence, tradeable, reason | **Yes** — the ONE place action/entry/SL/target are decided | Confidence is additive here (proven ~0 correlated with outcome) |
| 10. Action gate | line 588 | `signal["action"]` | — | **Yes** — `NO_TRADE` unless `"BUY CE"/"BUY PE"` | No |
| 11. Probability | `_calibrated_probability()` (594) | signal["confidence"] | `probability`, `probability_note` — real historical win-rate bucket, honestly `None` below `CALIBRATION_MIN_SAMPLE=5` | No | A real calibration attempt, distinct from `confidence` — see `PROBABILITY_CALIBRATION_AUDIT.md` for how uncalibrated it turns out to be |
| 12. Risk score | `_compute_risk_score()` (595-597) | entry_price, sl_price, capital, risk_pct | `risk_score` int | No | No |
| 13. Regime shadow | `regime_profile.classify_market_regime()` (606-626) | direction, confidence, rows, atm, underlying, support, resistance, market_structure, expiry_date, is_mcx | `regime_assessment` attached to Recommendation | **No — SHADOW ONLY**, `TI_ENABLE_REGIME_FILTER_SHADOW` default OFF | No |
| 13b. Failure gate shadow (**uncommitted**) | `failure_gate.run_failure_checks()`, wired only in `/root/oi_dashboard/.claude/worktrees/build-failure-gate` | same inputs as step 13 | `FailureReport` (CLEAR/BLOCKED per-check) attached to Recommendation | **No — SHADOW ONLY**, `TI_ENABLE_FAILURE_GATE_SHADOW` default OFF, **not on `main`** | No — real, tested (25+2 tests), not part of the canonical path today. Full design mapping in `FAILURE_FIRST_GATE_DESIGN.md`. |
| 14. Institutional findings | `institutional_intelligence.analyze()` (628-631) | symbol, snapshot, underlying, expiry_date | `findings` list | No | Reasoning-only |
| 15. Quantity sizing | `position_sizing.compute_quantity()` / `adaptive_sizing.compute_adaptive_quantity()` (633-649) | entry_price, sl_price, capital, risk_pct | `qty` | Indirect — sizing to 0 is a deliberate skip | No |
| 16. Reasoning sections | `_reasoning_sections()` (652-655) | signal, pcr, findings, atm_row, price_trend_pct, market_structure | 4 text fields | No | No |
| 17. **Final assembly** | `Recommendation(...)` (658-671) | everything above | The one immutable output object | — | — |
| 18. Signal log | `_log_signal()` → `ti_store.record_signal()` (157-172, wraps every return path) | rec | `ti_signal_log` row | No | Audit trail only |

**Downstream of `evaluate()`'s return value**, traced in `api.run_scheduled_cycle()`:
- `paper_trading.enter_from_recommendation()` — `api.py:266`, the ONLY call site repo-wide. Gated on `action in ("BUY CE","BUY PE")`. `agents/risk_manager/risk_decision.py`'s account-level gate can also block it.
- `telegram_notifier.send_trading_intelligence_signal()` — `api.py:328`, the ONLY call site repo-wide. Gated on action + `confidence >= TI_TELEGRAM_MIN_CONFIDENCE` (75 by default).
- `signal_graph.run_shadow()` — shadow-only, confirmed to call `evaluate()`'s own result, never recompute a decision.
- `execution_state.create_execution()` — shadow-only observability.

`run_scheduled_cycle()` has exactly two real callers: `agents/runtime/agent_runtime.py:207` (the automated scheduler, confirmed active) and `app.py:6097`/`trading_intelligence_cli.py:81` (manual route/CLI triggers).

## Phase 2 — Canonical decision authority: confirmed

`ai_trading_engine.evaluate()` → `oi_engine.generate_signal()` is the **sole** path that can open a real `ti_paper_trades` row or send a real Telegram signal — re-verified by grep this pass (single call site each), consistent with the Phase 1 audit.

**Non-canonical signal producers** (unchanged from `ENGINE_REGISTRY.md` — not modified, not deleted):

| Producer | Why it exists | Reaches Telegram? | Reaches paper trading? | Reaches live trading? | Duplicates the canonical signal? |
|---|---|---|---|---|---|
| `engine_v2.compute_v2_trend_and_signal()` | Separate strategy, own `/engine-v2` page | No | No | No (structurally cannot) | No |
| `sr_engine_v3`'s `trade_decision` | ATR/OI-cluster S/R, own `/engine-v3` page | No | No | No | No |
| `dynamic_sr_engine` + `exit_engine_v4` | PDH/PDL ladder, own `/dynamic-sr` page; exit logic only runs in `backtest.py` | No | No | No | No |
| `ichimoku_engine.analyze()` | Standard Ichimoku, own advisory panel + own separate paper-trade table | No | Own table only | No | No |
| `scalping_engine.generate_scalp_signal()` | Momentum-acceleration scalp, advisory panel | No | No | No | No |

All five self-documented advisory/display-only, none independently reaches Telegram or `ti_paper_trades`. No deletion recommended, per the explicit instruction not to modify or delete these.

## Phase 3 — Signal contract

See `SIGNAL_CONTRACT.md` for the full field-by-field design. Headline: `confidence` and `probability` are already two distinct, never-conflated fields — one real existing strength this design preserves rather than "fixes."

## Target architecture vs. current state

```
                 MARKET DATA
                      |
                      v
              DATA VALIDATION           <- PARTIAL: snapshot.available exists; no
                      |                    explicit staleness/malformed check
               invalid -----> NO TRADE     (DATA_INVALID/DATA_STALE, see FAILURE_TAXONOMY.md)
                      |
                      v
             FEATURE GENERATION         <- EXISTS: market_structure.py (ATR, swing, PDH/PDL, VWAP)
                      |
                      v
            MARKET STRUCTURE            <- EXISTS but TWO vocabularies (detect_bias() bias string
                      |                    vs. classify_market_regime()'s regime string) --
                      v                    not yet reconciled to one BULLISH/BEARISH/NEUTRAL/
                OI / FLOW                  TRANSITION vocabulary (FAILURE_FIRST_GATE_DESIGN.md L2)
                      |
                      v                  <- EXISTS but folded additively into confidence,
             TRAP / CROWDING               not an independent veto (generate_signal() dual-
                      |                    source check); OI alone already cannot single-
                      v                    handedly create a trade today (bias must also agree)
          FAILURE-FIRST VETO
                 |         |             <- ZERO implementation anywhere (ARCHITECTURE_AUDIT.md);
              FAIL        PASS              explicitly a hypothesis, not attempted here
               |            |
            NO TRADE         v           <- PARTIAL: failure_gate.py (uncommitted) covers R:R
                       CALIBRATED           floor + confidence threshold + regime + level-
                       PROBABILITY          proximity independently -- roughly 1.5 of 8 levels
                            |               in the target hierarchy (FAILURE_FIRST_GATE_DESIGN.md)
                            v
                     EXPECTED VALUE      <- EXISTS but NOT CALIBRATED: _calibrated_probability()
                            |               measured at ~23-25% real rate regardless of bucket
                     negative -> NO TRADE  (PROBABILITY_CALIBRATION_AUDIT.md)
                            |
                            v
                    ENTRY VALIDATION    <- ZERO implementation (EXPECTED_VALUE_GATE_DESIGN.md);
                            |              correctly not buildable until probability is real
                            v
                   SL/TARGET VALIDATION  <- EXISTS: oi_engine.generate_signal()'s SL/target math
                            |              is structurally sound (2,405 real trades verified);
                       invalid -> NO TRADE  target-blocked-by-intervening-level check MISSING
                            |
                            v
                  CANONICAL SIGNAL ID   <- MISSING (signal_id field proposed, not implemented)
                            |
                            v
                     DEDUPLICATION      <- PARTIAL: open-position guard exists, race-condition
                            |              gap identified (PAPER_TRADING_DUPLICATION_AUDIT.md)
                            v
                  SIGNAL STATE MACHINE  <- PARTIAL: execution_state.py's 13-state machine is close,
                            |              missing a VETOED terminal state (TELEGRAM_SIGNAL_INTEGRITY.md)
                 +----------+----------+
                 v                     v
             PAPER                   LIVE          <- LIVE: structurally impossible (NullBrokerExecutor,
                 |                     |               AST-verified) -- correct and unchanged
                 +----------+----------+
                            v
                   CANONICAL TELEGRAM   <- EXISTS and already clean: one real call site,
                            |              already a pure delivery layer (TELEGRAM_SIGNAL_INTEGRITY.md)
                            v
                     EXIT / OUTCOME     <- PARTIAL: same entry/SL/target values used at exit
                            |              (good), but NO time-exit boundary live unlike backtest
                            v              (BACKTEST_PAPER_LIVE_EXIT_CONTRACT.md)
                     CALIBRATION DATA   <- EXISTS: ti_signal_log/ti_paper_trades capture outcomes;
                                            the calibration computed FROM them is proven inaccurate
```

**Reading this honestly**: roughly half the target pipeline already exists in some form; almost nothing exists as an independent, non-additive veto. The two biggest, best-evidenced gaps are (1) no failure-first veto stage exists separate from the additive confidence score, and (2) the "probability" available to feed any future EV gate is measurably not calibrated. Both are now quantified, not just suspected.

## Cross-reference: the full document set (this PR)

1. **`CANONICAL_SIGNAL_ARCHITECTURE.md`** (this document) — synthesis, decision trace, target-vs-current mapping
2. **`FAILURE_FIRST_GATE_DESIGN.md`** — the 8-level hierarchy mapped against what exists and against the uncommitted `failure_gate.py`
3. **`SIGNAL_CONTRACT.md`** — the canonical signal schema, field by field
4. **`PAPER_TRADING_DUPLICATION_AUDIT.md`** — the TOCTOU race, identity rule, minimum fix design
5. **`TELEGRAM_SIGNAL_INTEGRITY.md`** — confirmed single delivery path, state-machine gap
6. **`BACKTEST_PAPER_LIVE_EXIT_CONTRACT.md`** — the live time-exit gap
7. **`PCR_EXTREMITY_ISOLATION_REPORT.md`** — real ON/OFF ablation, HARMFUL in aggregate, mixed per-symbol
8. **`EXPECTED_VALUE_GATE_DESIGN.md`** — why it can't be built yet, and what would need to change first
9. **`PROBABILITY_CALIBRATION_AUDIT.md`** — the ~23-25%-regardless-of-bucket finding
10. **`FAILURE_TAXONOMY.md`** — 16 failure codes, honestly classified by current evidence

## Phase 21 — Implementation policy (per the explicit instruction: document only, do not build)

**Exact minimal future changes**, if and when a next phase is separately approved, in likely dependency order:
1. Merge `failure_gate.py` as-is (already built, tested, shadow-only, zero live-behavior change by construction) to stop losing the work — this alone changes nothing observable.
2. Add a live time-exit boundary to `_check_open_trade_exit()` (closes the exit-contract gap) — needs its own backtest validation first.
3. Add the `ti_paper_trades` partial-unique-index fix (closes the duplication race) — low-risk, mechanical, needs a migration.
4. Everything else (crowding/trap, EV gate, real probability calibration, structured failure taxonomy as live codes) is blocked on evidence that doesn't exist yet and should not be sequenced until it does.

**Tests required**: every item above already has a testing precedent in this repo (unit tests for pure functions, `ti_db` fixture-based integration tests for DB-touching changes, a before/after backtest for anything touching live signal-affecting behavior) — no new testing framework needed.

**Migration sequence**: documentation (this PR) → separate approval → `failure_gate.py` merge (no schema change) → time-exit change (no schema change, needs backtest first) → duplication fix (schema migration, additive column/index only, same pattern as every prior `ALTER TABLE ... ADD COLUMN` migration in this repo).

**Rollback plan**: every proposed change is additive and flag-gated or index-only — reverting means flipping a flag back to its default (`false`) or dropping an added index/column, never a destructive change to existing data. Matches this repo's own established migration discipline throughout this session's work.

## STOP

This concludes the investigation and documentation phase. No code was changed. The next implementation phase requires separate approval, per the explicit instruction this document set was produced under.
