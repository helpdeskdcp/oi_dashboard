# Milestone 10 — Final Institutional-Grade Review

Status: implemented, deep-reviewed (twice), UI-extended, scheduler-integrated, validated. Committed to `worktree-m10-trading-intelligence` only. **Not merged to `master`.** Per explicit instruction: this is the final review before merge approval — wait for explicit go-ahead.

This document is the third and final layer on top of `AI_TRADING_INTELLIGENCE.md` (architecture/data-flow/original-build) and the first review pass already recorded there. It covers exactly what was requested for this final pass: fix-or-document every remaining weakness, extend the UI to surface every new API field, decide and implement scheduler integration, formalize the probability-calibration framework, audit every AI score for consistency and magic constants, and re-run the full validation suite.

## 1. Remaining weaknesses: fixed or documented

Five weaknesses were listed at the end of the first review pass. Each was re-examined; two were fixed as UI/scheduler work (sections 3 and 4 below), one was fixed as a genuine bug found during this pass, and two are documented as intentionally unchanged because fixing them would require an architecture change, not a bug fix.

| # | Weakness | Disposition |
|---|---|---|
| 1 | UI didn't surface the new fields | **Fixed** — see section 3. |
| 2 | Gamma Trap / Institutional Buying-Selling never backtested | **Left unchanged, documented.** Backtesting requires a real, sizeable history of paper-trade outcomes attributable specifically to these two heuristics. That history doesn't exist yet (paper trading only started with this milestone) and fabricating one would violate the project's own "never fabricate market data or outcomes" rule. This isn't a code fix — it's waiting on real time passing with the engine running. Revisit once `ti_paper_trades` has a meaningful sample size. |
| 3 | Probability calibration starts empty | **Left unchanged by design, made transparent.** Starting empty and refusing to guess is the correct, honest behavior — the alternative (a fabricated prior) is exactly what this project's anti-fabrication rule forbids. What WAS fixed: the calibration process itself is now inspectable via `ai_trading_engine.calibration_report()` (section 5) rather than only inferable by reading source. |
| 4 | Not wired into the Milestone 9 scheduler | **Fixed** — see section 4. |
| 5 | Composite scores (AI Strike Score, Risk Score, Support/Resistance Strength) are hand-weighted, not fitted models | **Left unchanged by design, audited for correctness.** Fitting these to real outcomes would require a training pipeline and a real outcome dataset that doesn't exist yet (same root constraint as #2) — a genuine architecture addition, not a same-milestone fix. What WAS fixed: a real bug in `_strength()`'s normalizer meant the documented 0-100 range was never actually reachable (capped ~82) — see section 5. All weights are now asserted/documented rather than bare literals. |

**A sixth issue was found during this pass, not on the original weakness list:** `ti_store.record_signal()` — the writer for the `ti_signal_log` table — existed and was unit-tested since the original build, but nothing in `ai_trading_engine.evaluate()` ever called it. `AI_TRADING_INTELLIGENCE.md`'s own claim ("every AI Trading Engine recommendation... logged for explainability") was false in practice: the table was real but permanently empty. Fixed by wiring `_log_signal()` into all five of `evaluate()`'s return paths (BUY, HOLD, and every NO_TRADE variant). Regression-tested (`TestSignalLogging`, 4 tests).

## 2. Final architecture review

No architectural changes were made this pass — the module boundaries, safety invariant, and reuse-over-reimplementation discipline from the first two passes are unchanged. What changed is purely additive:

- `ai_trading_engine.py`: `_log_signal()` wired into every return path; `calibration_report()` added; `_RISK_SCORE_WEIGHTS` named.
- `strike_intelligence.py`: `_strength()`'s normalizer fixed to a mathematically-derived constant (`_STRENGTH_MAX_RAW`) instead of an unexplained literal; `_SCORE_WEIGHTS` sum asserted at import time.
- `api.py`: `run_scheduled_cycle()` added — the one new orchestration function this pass introduces, and the only piece of new "architecture," in the sense that it's a new callable surface. It does not duplicate `get_symbol_overview()`; it reuses the exact same snapshot → institutional-intelligence → evaluate() sequence and adds one thing a manual dashboard load doesn't do: calling `paper_trading.enter_from_recommendation()` when the recommendation is actionable.
- `agents/runtime/agent_runtime.py`, `agents/runtime/scheduler.py`, `agents/config.py`: additive scheduler registration (see section 4) — no existing agent's registration, cadence, or gating logic was touched.
- `templates/trading_intelligence.html`: additive UI sections (see section 3) — no existing section was removed or restructured.

**Data flow is unchanged from the first review pass's diagram** in `AI_TRADING_INTELLIGENCE.md` — `run_scheduled_cycle()` is a new entry point into the SAME pipeline (`market_data.get_snapshot()` → `institutional_intelligence.analyze()` → `ai_trading_engine.evaluate()`), not a parallel one. The one new edge is `evaluate() → paper_trading.enter_from_recommendation()`, which now happens automatically on the scheduled path (it already existed as a callable a human/route could invoke manually).

## 3. UI extension

`templates/trading_intelligence.html`'s per-symbol panel now renders every field the final review requested, verified by an end-to-end request through a real (test-only, non-production) authenticated Flask session — not just by reading the template source:

- **AI Signal table**: Market Bias, Confidence, Probability, Risk Score, Entry, Stop Loss, Targets (T1 / T2 / T3 — replaces the old single "Target" cell), Expected Move, Time Horizon, Qty.
- **New "AI Reasoning" table**: Institutional, OI, Greeks, and Price Action reasoning, each its own labeled row (previously only the single combined `reasoning` string was shown).
- **New "Strike Intelligence" table**: Strike, AI Strike Score (color-coded: green ≥70, yellow ≥40, grey below), CE/PE Build-up Type, OI Wall (Support/Resistance), Max Pain marker.

Verification method: a throwaway SQLite DB (not `oi_history.db`) was built with a realistic 5-strike NIFTY chain, a real admin session was created via Flask's test client (`session_transaction`, matching this repo's own `test_manual_trading.py` convention), and both `/api/trading-intelligence/overview` (200, all new `Recommendation`/`StrikeIntelligence` fields present in the JSON) and `/admin/trading-intelligence` (200, all new section headers present in the rendered HTML) were fetched for real. Nothing about this touched the live broker or production data.

## 4. Scheduler integration

**Decision: integrate.** Researched the existing Milestone 9 registration pattern first (how `risk_manager` — the closest precedent, since it also periodically *writes* state, a portfolio snapshot — registers with `agent_runtime.py`) before writing any code.

- `agents/trading_intelligence/api.py::run_scheduled_cycle()` — one cycle across every `config.TI_WATCHED_SYMBOLS` symbol: snapshot → institutional intelligence → `evaluate()` (which already auto-closes any open paper trade whose target/SL was hit) → `paper_trading.enter_from_recommendation()` if the recommendation is an actionable BUY.
- `agents/runtime/agent_runtime.py` — `"trading_intelligence"` added to `RUNTIME_AGENT_NAMES` and `_CYCLE_FUNCS` via a `_trading_intelligence_cycle()` wrapper, following the exact same shape every other registered agent uses (findings list, execution bookkeeping via the existing `sysadmin_store` table, existing failure-counter/escalation path — nothing new needed there).
- `agents/config.py` — `RUNTIME_CADENCE_SECONDS["trading_intelligence"] = 180` (3 minutes, matching the engine's own native 3-minute candle granularity — running more often than the underlying data changes would just re-evaluate the same stored cycle).
- `agents/runtime/scheduler.py` — `"trading_intelligence"` added to `_MARKET_SESSION_GATED_AGENTS` (the same set `trading_supervisor` is already in), so the cycle never fires outside NSE market hours against stale LTPs.

**Deliberately NOT added to `agents/sys_admin/orchestrator.py`'s `AGENT_NAMES`** (the Milestone 8 enable/disable registry, scoped to four specific agents). Two reasons: (a) that file is a previous milestone's, and the standing instruction is not to rewrite previous milestones; (b) it isn't necessary — `agent_runtime.run_agent_cycle()` already treats any agent not in `orchestrator.AGENT_NAMES` as "always enabled" (the same treatment `"memory"` and `"sys_admin"` already get), and a failing cycle is already caught, recorded, and escalated by `agent_runtime.py`'s own existing logic uniformly, regardless of orchestrator membership. This is documented in `agent_runtime.py`'s own module docstring, not just left implicit.

**What this actually means for safety**: the only side effects of an autonomous cycle are `ti_paper_trades` and `ti_signal_log` rows — the exact same tables and functions a human clicking through the dashboard already writes to. No new code path touches the broker; this is covered by the SAME `test_safety.py` AST scan as every other function in this package, since `run_scheduled_cycle()` only calls existing, already-scanned functions.

Regression-tested end to end (not just registration-tested): `TestRunAgentCycle::test_trading_intelligence_cycle_runs_with_no_data_and_reports_honestly` and `...opens_a_paper_trade_from_a_real_buy_signal` in `test_agents/runtime/test_agent_runtime.py`, plus the full existing 130-test `test_agents/runtime/` suite re-run to confirm zero interference with the other six agents' registration, cadence, or escalation behavior.

## 5. Probability calibration framework

**No new architecture was needed — the framework already existed, it just wasn't inspectable.** `_calibrated_probability()` has queried `ti_store.list_closed_trades()` live on every call since the first review pass: there is no cache, no separate "training" step, and no manual retraining trigger required. The moment a paper trade closes (via `evaluate()`'s own auto-close-on-target/SL path, or a human calling `paper_trading.close_and_journal()`), the very next `evaluate()` call for a signal in that trade's confidence bucket reflects it — automatically, by construction, not by a scheduled retrain.

What this pass adds is `ai_trading_engine.calibration_report()`: one row per confidence bucket (`0-39`, `40-59`, `60-79`, `80-100`), each showing `sample_size`, `wins`/`losses`, `min_sample_required`, and either a real `probability_pct` or an honest `None` with the exact reason (insufficient sample). This makes the "engine learns from its own paper trades" claim independently verifiable — a dashboard or a human can call it and see precisely what evidence backs (or doesn't yet back) each bucket's probability, rather than trusting the claim on faith. **No fabrication**: a bucket that has never had 5+ closed trades reports `None`, exactly as before — `calibration_report()` doesn't change what gets returned by `evaluate()`, it only exposes the same live computation for inspection.

Why "stored in Memory" doesn't mean `agents.memory` specifically here: the trade *journal* (`agents.memory`'s `agent_memory_trade_journal` table, via `paper_trading.record_journal_entry()`) is a human-readable explainability record, written only when a human supplies a `learning` note during `close_and_journal()`. The calibration's actual source of truth is `ti_store.list_closed_trades()` (every closed trade, always, regardless of whether a human ever journals it) — the more complete and more reliable of the two, and the one already used. Using the journal instead would mean calibration silently missed any trade nobody bothered to annotate, which would be a worse design, not a better one.

Regression-tested: `TestCalibrationReport` (3 tests) — one row per bucket, honest `None` for an empty bucket, and a real 66.7% figure recomputed correctly the moment 6 real trades (4 wins / 2 losses) exist in the 80-100 bucket.

## 6. AI Score audit: consistency, explainability, magic constants

All three scores use the same conventions (0-100 scale, higher score = more of the thing being measured, weights that are named constants rather than inline literals) but are computed from genuinely different evidence, which is itself documented so a reader doesn't assume they're interchangeable:

| Score | Scale | Owned by | Composed of |
|---|---|---|---|
| **AI Strike Score** | 0-100, higher = more "in play" right now | `strike_intelligence._ai_strike_score()` | `_SCORE_WEIGHTS` = OI wall 30% + conviction 25% + gamma exposure 20% + ITM contestedness 25% (asserted to sum to 1.0 at import time — not just eyeballed) |
| **Risk Score** | 0-100, higher = riskier | `ai_trading_engine._compute_risk_score()` | `_RISK_SCORE_WEIGHTS` = position-sizing infeasibility 60 + stop-as-%-of-premium 40 (now named; previously bare `60`/`40` literals) |
| **Confidence** | 0-100, higher = more rule-agreement | `oi_engine.generate_signal()` (Milestone 1-era, NOT owned by this milestone) | Out of scope to modify (`oi_engine.py`'s own "never duplicate this logic elsewhere" rule) — verified to already use the same 0-100 convention, confirmed by reading its own source, so nothing here disagrees with it |

**A real bug was found and fixed during this audit**: `strike_intelligence._strength()` (Support/Resistance Strength) divided by a bare `1.7` that didn't match the formula's actual maximum. The true ceiling — `share_pts` capped at 70, conviction factor capped at `(1 + 1.0)`, product 140 — should normalize by `140/100 = 1.4`, not `1.7`. With the old constant, even the single strongest possible real signal (100% OI share + fresh Long/Short Buildup) could only ever score ~82, silently narrower than the field's own documented 0-100 range. Fixed by deriving `_STRENGTH_MAX_RAW` directly from `_STRENGTH_SHARE_CAP_PTS` and `_STRENGTH_CONVICTION_MULT`'s own max value, so the normalizer is provably correct rather than asserted. Regression-tested: `TestStrengthCeiling::test_maximum_share_and_conviction_reaches_100` constructs the true extreme input and asserts the output is exactly 100.

**One deliberate, now-documented inconsistency**: `_CONVICTION_POINTS` (AI Strike Score's conviction term) scores `Neutral` build-up as `0`, while `_STRENGTH_CONVICTION_MULT` (Support/Resistance Strength) scores `Neutral` as `0.5`. This is NOT an oversight — the two ask different questions. AI Strike Score's conviction term asks "how much directional information does this build-up carry" (Neutral: genuinely none, correctly 0). Support/Resistance Strength asks "how much does this strike's raw OI back a level" (a Neutral-signal strike's OI is still real OI, still provides some backing, correctly nonzero). Documented explicitly in `strike_intelligence.py`'s own constants block so a future reader sees this is a choice, not a bug.

No other magic constants were found in the reviewed scoring functions; `_probability_from_lean()`'s `50 + lean*20, clamped [10,90]` was already self-documenting from the first review pass and needed no change.

## 7. Validation (re-run in full this pass)

| Suite | Result |
|---|---|
| `test_agents/trading_intelligence/` | 118 passed (up from 109 after the first review pass — 9 new: 4 signal-logging, 3 calibration report, 2 strength-ceiling) |
| `test_agents/runtime/` (scheduler integration) | 130 passed (2 new trading_intelligence cycle tests, zero interference with the other six agents) |
| `test_agents/sys_admin/` | re-run alongside runtime suite, unaffected |
| Replay / Stress / Performance / Memory / Trading (from `test_validation.py`, first review pass) | still passing, unmodified this pass — no change to the code paths they exercise beyond what's already covered above |
| Full repository suite | see below |

Full repository suite result for this final pass: **1,220 passed, 1 xfailed** (up from 1,209 after the first review pass — the 11 new tests above), zero regressions.

## 8. Technical debt

Carried forward, honestly, rather than hidden:

- `templates/trading_intelligence.html` shows the AI Strike Score per strike but not every Strike Intelligence field (Support/Resistance Strength, Gamma/Delta Exposure, IV Rank, Premium Momentum, Probability of ITM) — the final review's explicit UI requirement was the fields it listed, all of which are now shown; the remaining Strike Intelligence fields are already in the JSON payload (verified serializable) and are a small, isolated follow-up if wanted.
- Gamma Trap / Institutional Buying-Selling remain unvalidated heuristics (see section 1, #2) — inherent to how new they are, not a code defect.
- No fitted/trained model anywhere in this milestone — every score is transparent, documented arithmetic (see section 6) by design, consistent with this project's broader anti-black-box convention, not a gap to be "fixed" so much as a stated design philosophy.
- `orchestrator.py`'s enable/disable kill-switch does not cover `trading_intelligence` (see section 4) — a deliberate, documented scope boundary, not an oversight; a genuine "add a 5th orchestrator-controlled agent" change would touch Milestone 8 code, which this pass was instructed not to do.

## 9. Production readiness score

**9.5 / 10** for what this milestone claims to be: a recommendation-and-paper-trading engine that runs safely, unattended, without ever touching a real broker or fabricating data.

Justification for not claiming a perfect 10: two categories of technical debt remain by design rather than by oversight (sections 1 #2/#5, 8) — they require real elapsed time with the system running (backtesting the new heuristics) or a genuine architecture addition (a fitting pipeline) that wasn't in scope for this review. Everything that WAS in scope for a code-level fix in this pass — the dead signal-log table, the strength-ceiling bug, the scheduler gap, the UI gap — was found and fixed, not just described.

## 10. Merge recommendation

**Recommend merge**, pending your review of this document. Zero regressions across three full validation passes (original build, first review, this final review), a UI that now surfaces every field this review asked for, a scheduler integration that was researched against existing precedent before being written (not bolted on), a probability-calibration framework that was already correct and is now inspectable, and an AI-score audit that found and fixed a real, previously-undocumented ceiling bug. Per your standing instruction, no merge has been performed — waiting on your explicit approval.
