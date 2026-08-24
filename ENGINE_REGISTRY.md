# Engine Registry

Full repository inventory, Phase 1 of the architecture audit requested 2026-08-24.
Read-only investigation — no code changed to produce this document. Every "Called
By" claim below was verified by grep against the real repository, not assumed
from filenames or docstrings.

**Scope note on "engine":** this repo has two different kinds of things that
could be called an engine — (1) genuinely separate **strategy features**, each
with its own live page/route and its own signal logic (`oi_engine`, `engine_v2`,
`sr_engine_v3`, `dynamic_sr_engine`, `ichimoku_engine`, `scalping_engine`), and
(2) **shared building blocks** reused across those features (`market_structure.py`,
`greeks.py`, `sr_probability_engine.py`). The audit below keeps that distinction
explicit rather than flattening everything into one list, because it changes
what "consolidation" should even mean here — see `ARCHITECTURE_AUDIT.md`.

## A. Signal-generating engines (each capable of producing a directional call)

| Engine | File | Responsibility | Called By | Live route/surface | Real trading consequence? | Duplicate/Overlap | Recommendation |
|---|---|---|---|---|---|---|---|
| **oi_engine (OI-driven)** | `oi_engine.py` | `generate_signal()`: bias→CE/PE, entry=ATM LTP, target/SL via delta-projected OI-wall distance | `ai_trading_engine.evaluate()` (`agents/trading_intelligence/ai_trading_engine.py:583`), `backtest.py:580` | `/admin/trading-intelligence` (via the Trading Intelligence subsystem) | **Yes** — real paper trades, real Telegram signals under the "IDaddy AI" brand | Contains 2 trend-meter functions (`compute_trend_meter` + `compute_new_trend_meter`), both called live side-by-side (`app.py:4750,4864`) — investigate merge | **KEEP** — this is the canonical production path; 4 backtest reports this session already validated its math (arithmetic correct, edge unproven — see `ENTRY_BIAS_SELECTION_REPORT.md`) |
| **Engine V2** | `engine_v2.py` | PDH/PDL-divisor S/R + own `compute_v2_trend_and_signal()` | `app.py:633,4648,8526,8661`, `backtest.py:622,1317` | `/engine-v2` (own page) | No — explicitly display-only in its own docstring | Independent 3rd-ish signal generator, but self-aware/documented as advisory | **KEEP** — real, separate, live feature; not a duplicate of oi_engine's job, a different strategy on its own page |
| **SR Probability Engine** | `sr_probability_engine.py` | Evidence-weighted breakout/reversal scoring; `compute_dynamic_targets_sl()` (2nd independent target/SL formula); `action_candidate` field | `app.py:624`, `backtest.py`, `engine_v2.py`, `scalping_engine.py`, `sr_engine_v3.py`, `agents/trading_intelligence/{regime_profile,institutional_intelligence,strike_intelligence}.py` | No dedicated route — a shared library `engine_v2.py` and others import | No (feeds display-only consumers) | Foundational — most-reused file in this batch; correctly avoided being re-duplicated by `sr_engine_v3.py` (which reuses its `compute_dynamic_targets_sl` directly) | **KEEP** — this is closer to a shared building block than a competing engine, despite the name; resolve the "V1" naming collision with `dynamic_sr_engine.py` below |
| **SR Engine V3** | `sr_engine_v3.py` | ATR/OI-cluster-weighted "institutional" S/R + own `trade_decision` | `app.py`, `backtest.py` | `/engine-v3` (own page) | No — advisory | Own docstring: runs "in PARALLEL to V1, Engine V2, and the Scalping Engine" — self-aware | **KEEP** — real, separate, live feature; already reuses `sr_probability_engine` correctly rather than reimplementing |
| **Dynamic S/R + Exit V4** | `dynamic_sr_engine.py` + `exit_engine_v4.py` | PDH/PDL ladder → BUY/SELL + confidence; paired adaptive exit manager (trailing/VWAP/momentum-fade/time exit) | `dynamic_sr_engine`: `exit_engine_v4.py` only. `exit_engine_v4`'s real functions (`open_position`/`manage_exit`): **only `backtest.py` and its own tests** — zero calls from `app.py` (verified by grep) | `/dynamic-sr`, `/api/dynamic-sr/<symbol>` | No — display-only, but see conflict note | Self-labels "(V1)" — same "V1" collision as `sr_probability_engine.py` | **KEEP with a flag** — real live feature, but `exit_engine_v4`'s actual exit logic never runs in production (only in backtest); `app.py` only imports it to expose tunable constants via a settings route. This is a genuine backtest/production divergence — see `ARCHITECTURE_AUDIT.md` §E |
| **Ichimoku Engine** | `ichimoku_engine.py` | Standard Ichimoku → BUY/SELL/STRONG BUY/STRONG SELL | `app.py`'s main scalping loop, every cycle | Advisory panel inside the main dashboard; own paper-trading track | No — explicitly advisory, gates nothing | Independent signal, but methodologically distinct (trend-following) from the OI-driven engines — not duplicative in intent | **KEEP** — already backtested per project memory (~breakeven, 44.7% WR, known sideways-market false-signal limitation) |
| **Scalping Engine** | `scalping_engine.py` | Fast-timeframe momentum-acceleration scalp signal | `app.py:635,4766` (`generate_scalp_signal`), same main loop | Advisory panel | No | Reuses `sr_probability_engine`'s premium-momentum/entry-trigger building blocks — good reuse discipline | **KEEP** — distinct methodology (momentum-acceleration vs. structural) |

**The honest count**: **7 distinct code paths can produce a directional
BUY/SELL-style call.** Of those, exactly **1** (`oi_engine.generate_signal()`,
via `ai_trading_engine.evaluate()`) has real consequences — real paper trades,
real Telegram distribution, the thing 4 backtest reports this session have been
validating. The other 6 are self-documented, verified-by-grep advisory/display
features, each on its own separate page. This is architecturally closer to "7
distinct strategy products sharing infrastructure" than "1 job duplicated 7
times" — see `ARCHITECTURE_AUDIT.md` for why that distinction matters for any
consolidation decision.

## B. The Trading Intelligence subsystem (`agents/trading_intelligence/`, 44 files)

This directory is the home of the one real production signal path. Verified:
**zero competing decision-makers exist inside it** — every file either feeds
`ai_trading_engine.evaluate()`, consumes its output, or is genuinely orthogonal
(persistence, notifications, analytics). Full per-file findings:

| File | Responsibility | Called By | Feature Flag (real .env state) | Overlap | Recommendation |
|---|---|---|---|---|---|
| `api.py` | `run_scheduled_cycle()` (the real per-symbol cycle), `get_overview()` (cached dashboard read) | Runtime scheduler (`agents/runtime/agent_runtime.py`, confirmed **active**, not manual-only — see note below); `/api/trading-intelligence/overview` | n/a | none | **KEEP** — canonical orchestrator |
| `ai_trading_engine.py` | `evaluate()`: wraps `oi_engine.generate_signal()`, adds probability/risk-score/qty/reasoning, builds `Recommendation` | `api.run_scheduled_cycle()`, `signal_graph.py` | n/a | Correctly delegates entry/SL/target math to `oi_engine` rather than reimplementing | **KEEP** — the one real "final signal" assembler |
| `institutional_intelligence.py` | OI-pattern findings (buildup, walls, max pain, gamma trap, liquidity sweep) | `api.get_symbol_overview()`, `run_scheduled_cycle()` | n/a (advisory findings, doesn't gate trades) | Reuses `oi_engine.oi_walls/calc_max_pain` rather than reimplementing | **KEEP** — already backtested (`INSTITUTIONAL_FLOW_BACKTEST_REPORT.md`) |
| `regime_profile.py` | `classify()` (volatility percentile + buildup persistence, unused by prod yet) / `classify_market_regime()` (chop/trend/breakout gate) | `classify_market_regime()` called inside `ai_trading_engine.evaluate()` | `TI_ENABLE_REGIME_FILTER_SHADOW` = **OFF** | Explicitly documented as a risk gate ON TOP of `oi_engine`'s decision, not a 2nd signal engine | **KEEP** (shadow) |
| `signal_graph.py` | LangGraph observability wrapper around `ai_trading_engine.evaluate()` | `api.run_scheduled_cycle()` | `TI_ENABLE_SIGNAL_GRAPH_SHADOW` = **OFF** | **None** — confirmed calls `evaluate()` directly, never recomputes a decision | **KEEP** (shadow) — directly answers "does this produce a competing decision": no |
| `trade_guardian.py` + `trade_guardian_graph.py` | Monitors an already-open position; tighten-only Smart SL/Target advisory | `app.py:8093` | `TI_ENABLE_TRADE_GUARDIAN_SHADOW` = **ON** | Different responsibility (monitoring, not entry generation) — docstring: "never generates a new signal" | **KEEP** |
| `structure_alerts.py` / `structure_overlay.py` / `structure_chart.py` / `structure_backtest.py` / `structure_tuning.py` | A **second, intentional** S/R system (`institutional_levels.py`'s weighted zones), deliberately disconnected from `oi_engine`'s entries | `agents/runtime/agent_runtime.py:228` (alerts), `/api/structure/<symbol>/overlay` | `TI_ENABLE_STRUCTURE_ALERTS`/`_TUNING` = **ON** | **Real, documented, intentional duplication** of S/R calculation — serves the Telegram structure feed + overlay panel, not trade entries | **KEEP** — this is the one duplication in the repo that's explicitly by design, not accidental |
| `virtual_trailing.py` | Shadow trailing-stop simulation for open paper trades | `agents/runtime/agent_runtime.py:257` | `TI_ENABLE_VIRTUAL_TRAILING` = **ON** | None — parallel shadow state, never writes the real trade | **KEEP** |
| `adaptive_sizing.py`, `position_sizing.py` (top-level) | Position-size scaling on top of `position_sizing.compute_quantity()` | `ai_trading_engine.evaluate()` when `sizing_mode="adaptive"` (not default); also `agents/risk_manager/risk_engine.py`, `agents/runtime/{policy_engine,workflow_engine}.py` — 5 real call sites total | conditional | None — single canonical sizing function, good | **KEEP** — but `position_sizing.py`'s own docstring claims "no live trading path exists for this engine today," which is **stale/wrong** (5 real live callers found) — fix the docstring |
| `timeframe_confirmation.py` | MTF (15m/30m/1h) alignment score | `ai_trading_engine.evaluate()`, adaptive-sizing path only | conditional | None | **KEEP** |
| `trade_quality.py` | Post-hoc "was this trade well-reasoned" scoring | `adaptive_sizing.py`, diagnostics | n/a | None | **KEEP** |
| `dual_probability_calibration.py` + `_backtest.py` + `_features.py` + `_labels.py` | A **second, independent** calibrated-probability model (logistic + isotonic regression, 7 evidence groups) | **Nothing in the live path** — `dual_probability_store.py` never built, no shadow node wired | not wired at all | Genuinely separate "probability" concept from `ai_trading_engine._calibrated_probability()`'s confidence-bucket scheme | **INVESTIGATE** (already known-blocked — see `DUAL_PROBABILITY_CALIBRATION_REPORT.md`: target model never validated, stop model validates 3/8 cases) |
| `momentum_confirmation_backtest.py`, `institutional_flow_backtest.py`, `structure_backtest.py` | Backtest tooling for specific flags, reusing `backtest.py`'s replay engine | CLI/manual | n/a | None — correctly reuse shared replay | **KEEP** |
| `data_access.py` | The only DB-read seam for this whole package | everything above | n/a | None — enforces single read path | **KEEP** — good pattern |
| `ti_store.py`, `paper_trading.py` | Paper-trade persistence + stats, reusing `backtest.compute_advanced_trade_stats()` | `ai_trading_engine.evaluate()`, `api.py` | n/a | Separate table from `app.py`'s other paper-trade tables (`paper_trades`/`scalp_paper_trades`/`v3_paper_trades`) — intentional, each engine owns its own table | **KEEP** |
| `execution_state.py` | State machine + live-LTP for Execution State UI | `api.py`, execution-state routes | `TI_ENABLE_EXECUTION_STATE_UI`/`_SHADOW` = **ON** | None | **KEEP** — hardened this session (PR #42) |
| `telegram_notifier.py` | All outbound Telegram formatting/sending | `api.py`, `structure_alerts.py`, `trade_guardian_graph.py`, `production_watchdog.py` | n/a | None | **KEEP** — fixed this session (PR #46) |
| `monitoring_center.py`, `performance_analytics.py`, `paper_trade_diagnostics.py`, `production_watchdog.py`, `strategy_registry.py`, `ai_live_snapshot.py`, `explainability.py`, `risk_filters.py` | Read-only dashboards/analytics over already-computed data | various UI routes | n/a | None; `risk_filters.py` explicitly not wired into any entry gate | **KEEP** |
| `market_data.py` | `get_snapshot()` — the shared per-cycle snapshot everything above reuses | everything | n/a | None | **KEEP** — correct single "fetch" seam |
| `candle_recorder.py` | In-process 1m/5m candle building from live ticks | `app.py`'s tick loop → `multi_timeframe.py` | n/a | None | **KEEP** |
| `broker_execution.py` | `NullBrokerExecutor` — structurally cannot place a real order | referenced by safety tests | n/a | None | **KEEP** — the AST-verified paper-only guarantee |
| `signal_graph_store.py`, `trade_guardian_store.py` | SQLite persistence for the two shadow graphs | their own modules | n/a | None | **KEEP** |

**Runtime scheduler correction**: prior session memory (`project_milestone11_complete_ti_scheduler_dormant.md`,
2026-08-11) said the TI cycle only ran via manual "Run Cycle" clicks. That is
now **outdated** — `RUNTIME_SCHEDULER_ENABLED=true` in the real `.env`, and
`agents/runtime/agent_runtime.py`'s own docstring confirms `trading_intelligence`
was deliberately removed from `NEVER_SCHEDULABLE_AGENTS` at Milestone 17. It
runs automatically now. Memory updated to reflect this (see below).

## C. Foundational math (no competing implementation, reused everywhere)

| File | Responsibility | Reused by | Verdict |
|---|---|---|---|
| `market_structure.py` | ATR, swing hi/lo, PDH/PDL/PDC, opening range, VWAP, classical pivots | 11+ importers across `agents/trading_intelligence/`, `app.py`, `institutional_levels.py`, `sr_probability_engine.py` | **KEEP** — pure math, correctly scoped, "does NOT drive trading signals" by its own docstring |
| `greeks.py` | Black-Scholes delta/gamma from NSE IV | `oi_engine.py` and others | **KEEP** — clean, well-scoped |
| `expiry_intelligence.py` | Centralized expiry resolution, expiry-day analytics | Extensively | **KEEP** — hardened this session (PR #38/#42) |
| `institutional_levels.py` | Weighted composite S/R (composes `market_structure` + `oi_engine` primitives) + role-reversal detection | `app.py`, `agents/trading_intelligence/{structure_tuning,structure_alerts,structure_backtest,structure_overlay}.py` | **KEEP** — good composition discipline; own docstring's claim ("`ai_trading_engine`/`oi_engine.generate_signal()` remain the ONLY place a BUY CE/PE is decided") is true only for the *paper-trading path*, not the whole app given the 6 other engines above — a useful audit finding in itself, not a bug |
| `candlestick_patterns.py` | Rule-based OHLC pattern recognition | `app.py` (display context) | **KEEP** — explicitly not a signal |
| `intelligence_orchestrator.py` | Read-only aggregation of already-computed engine outputs | `intelligence_history_cli.py`, `app.py`, `monitoring_center.py`, `ai_live_snapshot.py` | **KEEP** — deliberately aggregation-only; its own docstring documents a directly relevant precedent (an earlier milestone brief asked for a `backend/engines/*` directory structure that doesn't exist; the author correctly mapped the spec onto real files instead of creating placeholders) |

## D. Non-signal infrastructure (verified no overlap with signal generation)

`agents/risk_manager/` — portfolio risk gate (VaR/CVaR, position sizing), consumes trades, doesn't generate them; reuses `position_sizing.compute_quantity` rather than duplicating it.

`agents/quant_researcher/` — full autonomous strategy-discovery system (feature registry, hypothesis catalog, statistical validation, evolution, promotion, codegen) — hard-gated in `NEVER_SCHEDULABLE_AGENTS`, only runs via manual `run_research.py` CLI, **never live-affecting**. Built but this session's own backtest/isolation work (SL-target grid search, confidence-factor isolation) didn't use it — a process gap, not a bug.

`agents/dev_agent/`, `agents/intelligence_alerts/`, `agents/intelligence_history/`, `agents/llm_providers/`, `agents/memory/`, `agents/ops/`, `agents/runtime/`, `agents/shadow_mode/`, `agents/sys_admin/`, `agents/trading_supervisor/` — confirmed non-signal infrastructure by their own module docstrings (CI gates, observability, agent scheduling, LLM provider abstraction, system health).

## E. Data fetching / broker integration (pure acquisition, no signal math)

`nse_fetcher.py`, `bse_fetcher.py`, `history_engine.py`, `fetch_history.py`, `mcx_session_config.py` — all confirmed pure data acquisition or session-hours config.

**`nse_option_chain.py` (138 lines) is dead code** — verified by grep, imported by nothing anywhere in the repository. Superseded by `nse_fetcher.py`. **Recommendation: REMOVE** (after one more confirmation pass, per the "don't delete before dependency verification" rule — this document already did that verification, but flagging for explicit sign-off before deletion since removal wasn't authorized as part of Phase 1).

## F. One-off analysis/debug scripts (standalone, not imported by the live app)

`analyze_level_accuracy.py`, `analyze_level_accuracy_6month.py`, `analyze_sl_tightness.py`, `analyze_standard_formulas.py`, `analyze_swing_low_patterns.py`, `debug_vix.py`, `debug_natgas.py`, `check_nifty_today.py`, `explore_angelone_data.py`, `find_best_formula_coefficients.py`, `verify_data_fetching.py`, `backfill_market_structure.py` — none imported by `app.py` or `agents/`; each correctly reuses real production functions (e.g. `analyze_swing_low_patterns.py` reuses `app.py`'s own `find_reversal_points()`) rather than reimplementing.

**Flag worth following up**: `find_best_formula_coefficients.py`'s own docstring states the current S/R formula (PDH+Range/2 for resistance, PDH-Range/4 for resistance_reversal, mirrored for support — the formula `engine_v2.py`/`market_structure.py`-family code uses) was found **"consistently biased"** against 247 days of real evidence, and the script searches for a better divisor. It is **unverified whether this finding was ever applied** to the live formula. This belongs in `MATHEMATICS_REGISTRY.md` (Phase 3) as an open item, not resolved here.

## G. CLI entrypoints and scripts/ (thin wrappers, no independent logic)

`approve_cli.py`, `expiry_intelligence_cli.py`, `intelligence_alerts_cli.py`, `intelligence_history_cli.py`, `runtime_control_cli.py`, `shadow_mode_cli.py`, `structure_tuning_cli.py`, `trading_intelligence_cli.py`, `run_research.py` — all confirmed thin wrappers; each docstring states it's the intended invocation path for its target module. `structure_tuning_cli.py`'s underlying loop is also wired into the live TI cycle via `agent_runtime.py`, unlike its CLI siblings.

`scripts/hardening/{market_replay,performance_profile}.py`, `scripts/runtime/*.py` — one-off milestone validation/hardening scripts, not engines. `market_replay.py` calls `backtest.simulate_ichimoku_trades`, confirming `backtest.py` is a shared multi-engine backtest harness (separate `simulate_*` function per engine — `simulate_trades` for oi_engine, `simulate_ichimoku_trades` for Ichimoku, plus V2/V3/V4 variants) rather than duplicated backtest logic per engine. **5 backtest-named files total, no competing sibling to `backtest.py`**: `backtest.py` (top-level) + `structure_backtest.py`, `dual_probability_backtest.py`, `institutional_flow_backtest.py`, `momentum_confirmation_backtest.py` (all in `agents/trading_intelligence/`).

## H. Core app infrastructure

`app.py` (Flask app, ~9,000 lines — route layer traced in `ARCHITECTURE_AUDIT.md`), `auth.py`, `billing.py` (own `DB_PATH`, no signal logic), `manage.py`, `runtime_paths.py` (canonical single source of truth for prod file paths, exists because an earlier deployment guessed the wrong DB filename), `advisory_chatbot.py` (OpenAI-backed Q&A bot, informational only), `intelligence_models.py` (pure output-shape dataclasses, no logic of its own).
