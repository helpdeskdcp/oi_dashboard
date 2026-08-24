# Architecture Audit — Phase 1

Repository-wide inventory requested 2026-08-24, in response to the "IDaddy AI
— Architecture Verification & Engine Consolidation" brief. This covers only
Phase 1 (inventory, `ENGINE_REGISTRY.md`, dependency tracing) as instructed —
**no architectural changes are made in this document or alongside it.**
Full per-file evidence is in `ENGINE_REGISTRY.md`; this is the required
concise synthesis, answering the brief's own lettered questions directly.

**Method**: four parallel investigations covered (1) the 15 top-level
signal-adjacent engines, (2) all 44 files in `agents/trading_intelligence/`,
(3) every other `agents/` subdirectory plus `app.py`'s route layer and the
runtime scheduler, (4) every remaining top-level script/utility/CLI file —
426 Python files total across the repository. Every claim below citing a
caller, route, or "never called" was verified by grep against the real
repository (a sample of the most consequential claims was independently
re-verified before writing this document), not inferred from filenames or
docstrings alone.

## A. What architecture do we have now?

**Not one pipeline — seven distinct, mostly self-aware, non-competing
strategy features, plus one clean shared-math layer underneath them.**

Exactly one of those seven — `oi_engine.generate_signal()`, assembled by
`ai_trading_engine.evaluate()`, orchestrated by
`agents/trading_intelligence/api.py`'s `run_scheduled_cycle()` — has real
consequences: real paper trades, real Telegram signals under the "IDaddy AI
Trading Intelligence" name, and it's what this session's four backtest
reports (`ENTRY_SL_TARGET_BACKTEST_REPORT.md` →
`SL_TARGET_RETUNE_REPORT.md` → `ENTRY_BIAS_SELECTION_REPORT.md` →
`CONFIDENCE_FACTOR_ISOLATION_REPORT.md`) have all been investigating.

The other six — `engine_v2.py` (`/engine-v2`), `sr_engine_v3.py`
(`/engine-v3`), `dynamic_sr_engine.py`+`exit_engine_v4.py` (`/dynamic-sr`),
`ichimoku_engine.py`, and `scalping_engine.py` — are each genuinely live,
each on their own page or panel, each explicitly self-documented in their
own code as advisory/display-only. They are not accidental duplicates of the
production path; they're separate strategy experiments that happen to share
some underlying math (`sr_probability_engine.py`, `market_structure.py`).

**Within** the one production path (`agents/trading_intelligence/`, 44
files), the architecture is genuinely clean: zero competing decision-makers
were found. Everything either feeds `ai_trading_engine.evaluate()`, consumes
its output, or is orthogonal infrastructure (persistence, Telegram,
analytics). The two apparent duplications inside that directory — a second
S/R system (`institutional_levels.py`, for the Structure Alert/Overlay
feed) and a second probability model (`dual_probability_*`, never wired
in) — are both **documented, intentional, and already known** rather than
accidental sprawl.

## B. What architecture should we have?

Mapping the brief's declared pipeline onto what's found:

| Stage | Exists? | Where |
|---|---|---|
| Data quality / normalization | Partial | `market_data.get_snapshot()`, no explicit quality-gate stage |
| Market regime | Yes (shadow) | `regime_profile.classify_market_regime()`, gated `TI_ENABLE_REGIME_FILTER_SHADOW=OFF` |
| Multi-timeframe trend/structure | Yes | `market_structure.py` (foundational), `multi_timeframe.py`, `timeframe_confirmation.py` (adaptive-sizing path only) |
| Volatility | Yes | ATR inside `market_structure.py` |
| Dynamic SR zones | Yes (two, intentional) | `oi_engine.oi_walls()` (entries) / `institutional_levels.py` (Structure Alert feed) |
| OI / option-chain positioning | Yes | `oi_engine.py`, `institutional_intelligence.py` |
| Participant-behaviour / crowding / trap | **No** | Nothing computes a crowding score or runs a trap-confirmation state machine anywhere. Closest proxies: `oi_engine`'s signal-field classification ("Short Covering"/"Short Buildup"), `institutional_intelligence`'s findings — neither is a crowding score |
| Direction | Yes | `oi_engine.detect_bias()` — **this session already measured its real-world accuracy: 49.9% on 2,303 real signals, a coin flip** (`ENTRY_BIAS_SELECTION_REPORT.md`) |
| Setup / entry location | Partial | `oi_engine`'s structural-proximity check exists but is gated on `market_structure`/`underlying`, which the backtest path never supplies — untested (`CONFIDENCE_FACTOR_ISOLATION_REPORT.md`) |
| Failure check | **No** | No structured, itemized PASS/FAIL gate exists anywhere. `generate_signal()` has ad-hoc additive bonuses/penalties instead — see §L |
| SL / target | Yes | `oi_engine.generate_signal()` — arithmetic verified correct (0 invariant violations across 2,405 real trades), real-world edge unproven |
| Calibrated probability | Partial | `ai_trading_engine._calibrated_probability()` (confidence-bucket, real but relabeled confidence, not independently calibrated) vs. `dual_probability_*` (more rigorous, never wired in, partially validated at best) |
| Expected value | **No** | No module anywhere computes `P(target)×Reward − P(SL)×Risk` explicitly |
| Risk filter | Partial | `agents/risk_manager/` is a portfolio-level gate that consumes trades; not a per-signal NO_TRADE veto |
| Final signal | Yes | `ai_trading_engine.evaluate()` → `Recommendation` — the one real canonical assembler |
| Backtest/production parity | **No (one confirmed gap)** | `exit_engine_v4`'s real exit functions only ever execute inside `backtest.py` — zero live calls found. `app.py` only imports it for its tunable constants |

## C. What is already correct?

- `market_structure.py`, `greeks.py`, `expiry_intelligence.py`,
  `data_access.py` — pure, well-scoped, foundational, no competing
  implementation, correctly reused everywhere.
- `ai_trading_engine.evaluate()` correctly delegates entry/SL/target math to
  `oi_engine.generate_signal()` rather than reimplementing it.
- `institutional_intelligence.py`, `adaptive_sizing.py`,
  `timeframe_confirmation.py`, `trade_quality.py` all correctly build on top
  of the one real signal instead of competing with it.
- `signal_graph.py` confirmed (not assumed) to be pure observability — it
  calls `ai_trading_engine.evaluate()` directly and never recomputes a
  decision.
- `sr_engine_v3.py` correctly reuses `sr_probability_engine.compute_dynamic_targets_sl()`
  rather than adding a 4th target/SL formula.
- `broker_execution.NullBrokerExecutor` — the paper-only guarantee is
  structural, AST-verified by `test_agents/trading_intelligence/test_safety.py`.
- `oi_engine.generate_signal()`'s own arithmetic — proven internally
  consistent across 2,405 real backtested trades this session (SL always
  below entry, target always above, no exceptions).

## D. What is duplicated?

- **3–4 independent entry/SL/target formulas** across the app: `oi_engine`'s
  (delta-projected to nearest OI wall vs. a percent floor), `sr_probability_engine.compute_dynamic_targets_sl()`
  (delta-projected to next structural level, adaptive-swing SL), and
  `dynamic_sr_engine`'s fixed range ladder (already superseded for exits by
  `exit_engine_v4`'s adaptive version, per that module's own docstring: "V1's
  fixed range1-multiple ladder hit in only ~2% of trades").
- **Two S/R systems** (`oi_engine.oi_walls()` vs. `institutional_levels.py`)
  — documented, intentional, serving different surfaces.
- **Two probability models** (`ai_trading_engine`'s confidence-bucket vs.
  `dual_probability_*`'s logistic/isotonic model) — documented, the latter
  never wired in.
- **A real naming collision, not a functional duplication**: both
  `sr_probability_engine.py` (per `engine_v2.py`'s own docstring) and
  `dynamic_sr_engine.py` (per its own docstring) call themselves "V1."
- **`oi_engine.py` itself** contains two trend-meter functions
  (`compute_trend_meter` and `compute_new_trend_meter`), both still called
  live side-by-side (`app.py:4750,4864`, verified) — candidate for
  consolidation, not yet investigated for why both are kept.

## E. What is conflicting?

- The seven signal-generating features **can, in principle, disagree** on
  the same symbol at the same time (one panel showing bullish, another
  bearish) with no reconciliation layer — none of this is prevented today.
  Six of the seven have no real trading consequence, so this is a
  **presentation/trust consistency risk**, not a capital-risk one, but it is
  a real gap against the brief's "different modules must not produce
  contradictory decisions" principle.
- **`exit_engine_v4`'s exit logic never runs live** — its `open_position`/
  `manage_exit` functions are called only from `backtest.py` and its own
  test file (verified by grep: zero hits in `app.py`). `app.py` only
  imports the module to expose its tunable constants via a dev-settings
  route. This means any conclusion drawn from backtesting
  `dynamic_sr_engine`+`exit_engine_v4` together does not describe what
  actually happens to a live `/dynamic-sr` position — **what live exit
  logic (if any) actually governs those positions was not fully resolved
  by this pass and needs targeted follow-up**, not assumed either way.

## F. What is unused?

- **`nse_option_chain.py`** — confirmed dead by grep, zero importers
  anywhere in the repository. Superseded by `nse_fetcher.py`.
- **`dual_probability_*`** — built, never wired in (known, documented,
  blocked pending evidence per its own report).
- **`regime_profile.classify()`** — unused by production; only its sibling
  `classify_market_regime()` is called.
- **`research_framework.py`/`agents/quant_researcher/`** — built,
  functional, hard-gated from ever running live, but this session's own
  ad-hoc backtest/isolation scripts (the SL/target grid search, the
  confidence-factor isolation) didn't use this existing framework — a
  process gap worth naming, not a code defect.

## G. What should be merged?

- `oi_engine.py`'s two trend-meter functions — candidate, pending
  verification that the older one isn't still needed for a specific
  backward-compatibility reason.
- No cross-engine merge (e.g. folding `engine_v2`/`sr_engine_v3`/
  `dynamic_sr_engine` into `oi_engine`) is recommended — they are
  genuinely different methodologies (OI-driven vs. pure price-structure vs.
  Ichimoku vs. momentum-scalp) serving their own live pages, not the same
  job implemented four times. Merging them would delete real, distinct
  functionality, not just deduplicate code.

## H. What should be removed?

- **`nse_option_chain.py`** — the one confident removal candidate from this
  pass (dead, unimported, superseded). Not deleted here — flagged for
  explicit sign-off, per the brief's own "don't delete before dependency
  verification" rule; this document is that verification, not the deletion
  itself.
- Nothing else. Every other engine found is either live-and-used (has a
  real route/page) or documented-intentional-shadow — removing those would
  delete real user-facing functionality or working observability, not clean
  up cruft.

## I. What mathematical logic is already strong and must be preserved?

`market_structure.py`'s ATR/swing/PDH-PDL/VWAP/pivot math; `greeks.py`'s
Black-Scholes; `oi_engine.generate_signal()`'s entry/SL/target arithmetic
(internally correct, verified this session); `expiry_intelligence.py`'s
fail-closed expiry resolution (hardened this session); `data_access.py`'s
single-DB-read-seam pattern.

## J. What mathematical logic is unverified?

- **Every engine's confidence/probability score except the audit already
  done on `oi_engine`'s**: `sr_probability_engine`'s breakout_pct/
  reversal_pct, `sr_engine_v3`'s hold/break probability, `dynamic_sr_engine`'s
  0–100 confidence — none of these have been backtested for calibration.
  Given `oi_engine`'s own score turned out to have ~0 correlation with
  outcome (`ENTRY_BIAS_SELECTION_REPORT.md`), these should not be assumed
  better without the same evidence.
- **`find_best_formula_coefficients.py`'s finding**: the PDH/PDL-divisor S/R
  formula was measured "consistently biased" against 247 days of real data;
  unverified whether that finding was ever applied to the live formula.
- **`exit_engine_v4`'s adaptive exit logic** — never verified against live
  behavior since it never runs live (see §E).
- **`dual_probability_*`'s models** — already known partially validated at
  best (target model never validated at any horizon; stop model validates
  in 3/8 symbol/direction cases, one confirmed overfit).

## K. Where is the single canonical signal supposed to live?

For the "Trading Intelligence" / paper-trading / Telegram-signal feature —
almost certainly what "the IDaddy AI signal" means in practice, since it's
the only one with real consequences and the only one this session's evidence
chain has been about — **it already lives in exactly one place**:
`oi_engine.generate_signal()` → `ai_trading_engine.evaluate()` →
`api.run_scheduled_cycle()`. That's confirmed, not aspirational.

If the intent is one signal for the *entire application* (all seven
features unified), that does not exist and — per §G above — probably
shouldn't, since the other six are distinct strategies on distinct pages,
not redundant copies. The realistic target state is: **keep the one real
production signal singular** (it already is), and **explicitly label the
other six as "alternative/experimental strategies," not competing
candidates for "the" signal** — a naming/documentation clarification more
than a code change.

## L. What is preventing the current application from implementing the declared signal pipeline correctly?

Three genuine, specific gaps, in order of how actionable they are:

1. **No failure-first veto layer.** This is the most concrete, best-evidenced
   gap. `generate_signal()`'s confidence score is one additive point pile —
   this session already measured that its single most influential
   component (PCR extremity, firing on 78% of trades) is associated with
   *worse* outcomes, not better (`CONFIDENCE_FACTOR_ISOLATION_REPORT.md`).
   In an additive scoring system, a real red flag can be numerically
   outweighed by unrelated bonuses. The brief's "failure conditions checked
   independently → critical failure means NO TRADE regardless of score" is
   architecturally absent, not just weakly implemented.
2. **No expected-value gate.** Probability (of some kind) and R:R both
   exist as separate values; nothing combines them into an explicit EV
   check before a signal fires.
3. **No participant-behaviour/crowding/trap concept.** Confirmed nothing in
   the repository computes this today — the closest proxies
   (signal-field classification, institutional-intelligence findings) are
   not the same thing. If built, this would be genuinely new capability,
   not a hidden duplicate — consistent with the brief's own rule that new
   engines are justified only when "no existing component owns the
   responsibility."

None of these three are "the math is wrong" — they're "this stage doesn't
exist yet." That distinction matters for what Phase 2+ should actually
attempt.

## What this document does NOT do

No code, weight, formula, or route changed. No engine merged, deprecated, or
removed. This is the mandated first-phase inventory and synthesis only, per
explicit instruction to stop here for review before any Phase 2+ work
(dependency-graph deep dive, `MATHEMATICS_REGISTRY.md`, `SIGNAL_PIPELINE.md`,
`ENGINE_CONSOLIDATION_PLAN.md`, `SIGNAL_INTEGRITY_REPORT.md`, or any actual
consolidation).

## Required summary block

```
ARCHITECTURE STATUS
-------------------
Total engines/modules inventoried: 426 Python files (101 top-level, 163 agents/, 156 test_agents/, 6 scripts/)
Signal-generating engines found:   7 (1 production, 6 live-but-advisory)
Duplicate (intentional, documented): 2 S/R systems, 2 probability models
Duplicate (unresolved, needs a look): 2 trend-meter functions in oi_engine.py
Dead code found:                   1 file (nse_option_chain.py)
Conflicting (real gap):            exit_engine_v4 never executes live (backtest/production divergence)
Candidates for merge:              oi_engine's 2 trend-meter functions (investigate)
Candidates for removal:            nse_option_chain.py (pending sign-off)

SIGNAL STATUS
-------------
Multiple signal generators:         Yes — 7 total, 6 self-documented advisory-only
Single canonical signal generator:  Yes, for the one production feature — oi_engine.generate_signal() via ai_trading_engine.evaluate()
Entry source:                       oi_engine.generate_signal() (production); 3 other independent formulas exist for the 6 advisory features
SL source:                          same
Target source:                      same
Probability source:                 ai_trading_engine._calibrated_probability() (confidence-bucket); dual_probability_* exists, unwired
Confidence source:                  oi_engine.generate_signal()'s additive bonus/penalty score (proven ~0 correlation with outcome this session)
SR source:                          oi_engine.oi_walls() (entries) + institutional_levels.py (alerts/overlay) — intentional split
OI source:                          oi_engine.py + institutional_intelligence.py

MATHEMATICS STATUS
-------------------
Verified (this session):    oi_engine.generate_signal()'s entry/SL/target arithmetic (correct); market_structure.py's ATR/swing/pivot math; greeks.py's Black-Scholes
Needs verification:         sr_probability_engine/sr_engine_v3/dynamic_sr_engine confidence scores; find_best_formula_coefficients.py's bias finding (applied or not?); exit_engine_v4 live behavior
Known defects:               PCR-extremity confidence bonus runs backwards (measurably worse outcomes when it fires) -- not yet fixed, flagged for train/test-validated correction
Untested formulas:           5 of oi_engine's 9 confidence factors have never fired in any backtest (structural/regime/cross-verify/dual-source/order-flow -- gated on inputs the backtest never supplies)

STRATEGY STATUS
----------------
Backtest/production consistency: One confirmed gap (exit_engine_v4); oi_engine's path otherwise verified same-formula live and backtest
Lookahead risk:                   Not evaluated this pass (Phase 1 was inventory, not a lookahead-bias audit of every backtest module -- flagged as Phase 2+ scope)
Data leakage risk:                Same -- not evaluated this pass
Probability calibration:          Not calibrated (oi_engine's confidence proven ~0 correlated with outcome this session); dual_probability_* exists but only partially validated and unwired
NO TRADE handling:                Exists (generate_signal() returns NO_TRADE honestly on missing data/neutral bias) but no structured failure-first veto layer independent of the additive score
Failure detection:                Ad-hoc penalties inside the confidence score, not a separate gate -- the single most actionable architectural gap found (§L)

RECOMMENDATION
--------------
Do not build Phase 2-6 yet. This Phase 1 pass surfaced enough real, specific,
already-actionable findings (PCR-extremity backwards, exit_engine_v4 never
live, 5/9 confidence factors never backtested, one dead file, one function
pair to investigate merging) that the next useful step is deciding which of
THESE to act on -- each individually small and evidence-backed -- rather than
immediately committing to the full 6-document Phase 2-6 program. Recommend
reviewing this report and choosing a concrete next target (the failure-first
gate is the best-evidenced, most architecturally central gap) before
continuing the audit further.
```
