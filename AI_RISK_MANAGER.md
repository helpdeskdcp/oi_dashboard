# AI Risk Manager (Milestone 6)

Status: implemented, tested, not yet merged to `master`.

Mission: protect capital before profit, prevent bad AI-generated strategies from reaching production, continuously monitor live portfolio risk, and keep every risk decision fully explainable and auditable.

## Package layout

```
agents/risk_manager/
  risk_engine.py          Promotion Risk Gate math (pure functions, no I/O)
  risk_intelligence.py    memory-aware layer on top of risk_engine (the only
                           module here that touches agents.memory)
  gate.py                 RiskAssessment -> agents.dev_agent.gates.base.GateResult
  data_access.py          reads live paper-trading/wallet/Greeks data
                           (never a broker API call)
  portfolio_monitor.py    Live Portfolio Risk Monitor (uses data_access.py)
  risk_report.py          shared JSON + human-readable report shape
  risk_store.py           SQLite persistence (risk_assessments, risk_alerts,
                           risk_snapshots)
  api.py                  Risk API -- plain functions a Flask route calls
```

## 1. Promotion Risk Gate

Runs as **Gate 6**, appended after `agents.dev_agent.pipeline.run_gates()`'s original five, inside `agents.quant_researcher.research_engine._submit_for_approval()`. Every promoted `StrategySpec` must clear it before an `APPROVED`/`REQUIRES_REVIEW` decision reaches the audit log.

Checks, all in `risk_engine.py`:

| Check | What it verifies |
|---|---|
| `position_sizing` | `position_sizing.compute_quantity()` (reused, not reimplemented) sizes to >= 1 unit at the configured risk-per-trade % |
| `capital_allocation` | candidate + every currently-promoted strategy's risk % stays under `RISK_MAX_CAPITAL_ALLOCATION_PCT` |
| `exposure_symbol` / `exposure_sector` / `exposure_strategy` | same, grouped by symbol / `RISK_SYMBOL_SECTORS` / strategy family |
| `concurrent_trades` | total active strategies (incl. candidate) under `RISK_MAX_CONCURRENT_STRATEGIES` |

Plus, unconditionally computed and folded into the risk score (not pass/fail checks, but scored penalties):

- **VaR** / **CVaR (Expected Shortfall)** — historical simulation directly on the candidate's own backtested trade points.
- **Drawdown simulation** — bootstrap resampling (`RISK_DRAWDOWN_SIMULATION_TRIALS` trials) of the realized trade sequence; reports mean / Nth-percentile / worst simulated max drawdown.
- **Stress testing** — recomputes net P&L / drawdown under configured loss-amplification shocks (`RISK_STRESS_TEST_SHOCKS`).
- **Correlation analysis** — Pearson correlation of daily P&L against every other active strategy.

All of the above combine into a **0-100 risk score** (`compute_risk_score`), which maps to a decision via `RISK_SCORE_REJECT_BELOW` / `RISK_SCORE_REVIEW_BELOW`. `gate.py` maps `REJECTED -> GateStatus.FAILED` (hard-blocks the promotion, same as any other failing gate) and `APPROVED`/`REQUIRES_REVIEW -> GateStatus.PASSED` (the nuance is carried in `GateResult.details`, never silently dropped).

## 2. AI Risk Intelligence

`risk_intelligence.assess()` wraps `risk_engine.evaluate_promotion()` with real memory context:

- **Active strategies** (portfolio context for exposure/correlation) come from `agents.memory`'s `is_best=True` parameter sets.
- **Failure pattern detection** (`detect_failure_patterns`) flags a strategy family with more than one recorded failure.
- **"Never recommend a configuration that previously failed without explaining why"**: `compare_with_historical_failures` numerically compares the candidate's thresholds against every past failure's recorded thresholds (within `RISK_PARAMETER_SIMILARITY_PCT`). A match downgrades an otherwise-`APPROVED` decision to `REQUIRES_REVIEW` and appends an explicit explanation — it never silently promotes a near-repeat of a known failure, and never overrides an already-`REJECTED` decision.
- **Safer parameter recommendations** (`recommend_safer_parameters`) suggest moving each matched threshold further from the failed value.

## 3. Live Portfolio Risk Monitor

`portfolio_monitor.snapshot(user_id=None)` reads real (paper-trading) data via `data_access.py`:

- `paper_orders` (per-user) + `paper_trades`/`scalp_paper_trades`/`v3_paper_trades` (system-wide, no `user_id`) for open positions.
- `users.wallet_balance` for a specific user's available capital.
- `strikes`/`cycles` for Delta/Gamma/Theta/Vega — **read-only**, from whatever the live app already logged; this module never calls the Angel One session or triggers a live broker request.

Computes: real-time exposure, portfolio heat, margin utilization, daily realized P&L, intraday max drawdown, symbol concentration, cross-symbol price correlation (reuses `agents.quant_researcher.data_access.load_candles`), and Greeks exposure. Any threshold breach becomes a `RiskAlert`, published to `agents.event_bus` (its first real producer since Milestone 1) and returned in the snapshot.

**Safety invariant**: nothing in this file closes a position, cancels an order, or halts trading. Every "emergency recommendation" is a text recommendation attached to the alert — the same propose-only posture every other agent in this framework holds.

## 4. Audit & Explainability

`risk_report.RiskReport` is the one report shape used by both the promotion gate and the live monitor (`to_json()`, `human_readable()`). `risk_store.py` persists every risk decision:

```sql
risk_assessments  (promotion-gate decisions: risk_score, decision, full report JSON)
risk_alerts       (live-monitor breaches: metric, severity, value, limit, recommendation)
risk_snapshots    (periodic portfolio state: exposure, heat, margin, daily P&L, drawdown)
```

All three live in `oi_history.db`, same file as every other agent table, with indexes defined from the start (`idx_risk_assessments_symbol_ts`, `idx_risk_alerts_metric_ts`, `idx_risk_snapshots_user_ts`, etc.) and `PRAGMA busy_timeout=5000` on every connection.

## 5. Risk API + Dashboard

`agents/risk_manager/api.py` — plain, JSON-serializable functions (`get_portfolio_snapshot`, `get_recent_assessments`, `get_recent_alerts`, `get_recent_snapshots`). `app.py` exposes two new read-only routes:

- `GET /api/risk/portfolio` — the logged-in user's live snapshot (computes + persists).
- `GET /api/risk/alerts` — that user's recent persisted alerts.

`templates/manual_trading.html` gained one additive widget (`#risk-widget`) polling `/api/risk/portfolio` every 15s, showing portfolio heat % and active alert count, turning amber/red on a warning/critical alert. No existing element, route, or polling loop was modified.

## Migration notes

One schema change, fully backward-compatible and self-migrating:

- `agent_memory_backtest_history` gains an optional `trades_json` column (raw per-trade history, so `risk_intelligence.build_active_strategies()` can correlate a new candidate against another strategy's *real* trade history instead of only aggregate stats).
- **No manual migration step required.** `SQLiteMemoryStore.init_db()` (called automatically by `agents.memory.get_memory_store()`/`SQLiteMemoryStore.__init__`) checks `PRAGMA table_info(agent_memory_backtest_history)` and runs `ALTER TABLE ... ADD COLUMN trades_json TEXT` if the column is missing — the exact same migration pattern `app.py` already uses for its own schema evolution (`strikes`, `paper_orders`, `v3_paper_trades`, `paper_trades` all have equivalent `PRAGMA table_info` + conditional `ALTER` guards). Existing rows get `trades_json = NULL`; `record_backtest(..., trades=...)` is an optional keyword, so every pre-Milestone-6 caller keeps working unchanged.
- Three new tables (`risk_assessments`, `risk_alerts`, `risk_snapshots`) are created via `CREATE TABLE IF NOT EXISTS` by `risk_store.init_db()` — nothing to migrate, they simply don't exist until the first Risk Manager call.
- `agents/config.py` gained a new `RISK_*` settings block (position sizing, exposure limits, VaR/CVaR confidence, drawdown simulation, stress shocks, risk score thresholds, live-monitor thresholds) — every value has a sensible default and an environment-variable override, matching every other config block in this file. No existing setting was renamed or removed.

## Known, documented limitations (not placeholders — honest scope boundaries)

- **Cross-strategy correlation for the live monitor's *open* positions** is computed from underlying-symbol price correlation (via historical candles), not from two strategies' realized P&L series (which don't exist yet for a still-open position). Correlation for the *promotion gate* (closed backtested trades) uses real daily P&L series.
- **`risk_intelligence.build_active_strategies()`'s per-strategy trade lookup is best-effort by symbol**, not strategy name — `agent_memory_backtest_history` has no `strategy_name` column (a Milestone 4 design predating per-strategy promotions). When two strategies are active on the same symbol, correlation may be computed against a different (but same-symbol) strategy's trades. Documented in the function's own docstring; not a silent inaccuracy.
- **Capital is a configured constant (`RISK_ACCOUNT_CAPITAL`) for the Promotion Risk Gate** (there's no live account to ask before a strategy is ever live) but **real `users.wallet_balance`** for the Live Portfolio Risk Monitor's per-user view.
- **The two new Flask routes are covered by tests of the `risk_api` functions they call**, not by an end-to-end HTTP test — `app.py` is not imported in this test suite (it has real broker-session machinery at module scope; a documented landmine in this repo already caused one accidental live Angel One login via a test). Route wiring was verified via `py_compile`/Jinja syntax checks instead.

## Test summary

91 new tests across `test_agents/risk_manager/` (risk_engine, risk_intelligence, risk_report, risk_store, gate, data_access, portfolio_monitor, api) plus extensions to `test_agents/memory/test_sqlite_store.py` (3 tests: trades round-trip, no-trades-stored-as-None, migration-from-a-pre-Milestone-6-table) and `test_agents/quant_researcher/test_research_engine.py` (the promotion test now asserts the risk gate ran, ran last, and its assessment was persisted). Full repo suite: **743 passed, 1 xfailed**, zero regressions from the pre-Milestone-6 baseline of 649.
