# AI Trading Supervisor (Milestone 7)

Status: implemented, tested, not yet merged to `master`.

Mission: oversee all other BATI agents and the live trading workflow -- the cross-cutting layer above Milestones 3-6, which each own one slice of the pipeline (detect+patch, memory, research, risk).

## Package layout

```
agents/trading_supervisor/
  market_state.py          trend/range/volatility/expiry/event-risk classification
  agent_health.py           monitors dev_agent/quant_researcher/risk_manager + Memory
  conflict_detector.py       same-symbol, opposite-direction strategy conflicts
  data_health.py               stale-data / API-failure / broker-connectivity proxy
  supervision_engine.py         combines everything into one explainable verdict
  supervision_report.py          shared JSON + human-readable report shape
  supervision_store.py            SQLite persistence, indexed from the start
  gate.py                          Gate 7 -- wraps a verdict into a GateResult
  supervisor_agent.py               TradingSupervisor(BaseAgent), registered
```

## 1. First real `BaseAgent` adopter

`agents/base_agent.py` and `agents/registry.py` (Milestone 1) established "every agent subclasses `BaseAgent`, registers via `register_agent`" -- but Milestones 2-6 all shipped as free-standing function modules instead (`agents/dev_agent/pipeline.py`, `agents/quant_researcher/research_engine.py`). The pre-Milestone-6 architecture review flagged this as a Critical finding: dead foundation code, never actually used. `TradingSupervisor(BaseAgent)`, registered as `"trading_supervisor"`, is the first agent to actually use it -- `run_cycle()` returns `list[Finding]`, `on_event()` reacts to `agents.event_bus` events. This stops the drift; it doesn't retrofit Milestones 2-6 (a larger, separate change, flagged as follow-up work, same as the review's other Critical finding).

Fixed along the way: `test_agents/test_registry.py`'s isolation fixture wiped the registry to empty on teardown instead of restoring its prior contents -- harmless while no real agent used `register_agent`, but it would have silently unregistered `TradingSupervisor` for the rest of any test session that happened to run `test_registry.py` first. Fixed to snapshot/restore.

## 2. Gate 7: Trading Supervision

Appended after Gate 6 (`agents.risk_manager.gate`) in `agents.quant_researcher.research_engine._submit_for_approval()`. `supervision_engine.verify()` combines:

- **Risk-gate cross-check** (defense in depth): re-verifies that Gate 6 actually appears in `gate_results` and did not fail -- never trusts that risk approval happened just because this code path was reached, the same "re-verify the actual artifact" principle `agents/dev_agent/patcher.py`'s self-modification guard already holds itself to. Missing or failed risk gate -> hard `REJECTED`.
- **Conflicting signals** (`conflict_detector.py`): compares the candidate's direction against every other currently-promoted strategy on the same symbol. A same-symbol, opposite-direction pair -> `REQUIRES_REVIEW`. Direction is now stored inside `agent_memory_parameter_sets.parameters_json` (`agents.quant_researcher.research_engine._parameter_payload`, no schema change -- `parameters` was already a free-form JSON blob) so a strategy promoted before this milestone (no `direction` key) is skipped rather than guessed at.
- **Market state** (`market_state.py`): trend/range regime from `backtest.load_market_structure_snapshots` (the same live-computed ADX/ATR/regime data `app.py`'s own strategies use), volatility from INDIA VIX's own candle archive. Elevated volatility, or any dimension that couldn't be determined -> `REQUIRES_REVIEW`.
- **Data-feed health** (`data_health.py`): staleness of the most recent option-chain `cycles` row for the symbol, as an indirect broker-connectivity proxy -- **never** a live Angel One call.

"Trigger alerts instead of automatic execution when uncertainty is high" is the literal decision rule: any unresolved concern lands on `REQUIRES_REVIEW`, never a silent `APPROVED`. Same `GateStatus` mapping convention as Gate 6: `REJECTED -> FAILED`, `APPROVED`/`REQUIRES_REVIEW -> PASSED` with the full verdict carried in `GateResult.details`.

**Robustness fix caught by the first end-to-end test run**: `market_state.trend_range_regime()` and `data_health.check_feed_staleness()` initially let a real data-read failure (e.g. a missing table) propagate as an unhandled exception, which would have crashed the entire promotion pipeline -- exactly backwards for a module whose job is *detecting* abnormal conditions. Both now catch the failure and report `"unknown"`/`is_stale=True` instead.

## 3. Agent monitoring

`agent_health.py` reads `agents.audit_log.list_recent()` (new: agent + since_ts + limit filtering, same pattern as `list_pending`) for `dev_agent`, `quant_researcher`, `risk_manager` -- staleness (no recent activity) and failure clustering (disproportionate `rejected`/`failed` outcomes) are both surfaced, never conflated. Memory has no audit trail of its own (a passive store other agents write into), so its health is a direct reachability + freshness check instead.

## 4. `TradingSupervisor.run_cycle()`

A full sweep: every agent's health, every watched symbol's data-feed health and volatility state. Turned into `Finding`s (never a `ProposedAction` -- this agent verifies and alerts, it doesn't propose code or strategy changes), persisted to `supervision_store.agent_health_snapshots` for a complete audit trail, and (for `severity="critical"` findings) published to `agents.event_bus` -- its second real producer after `agents.risk_manager.portfolio_monitor` (Milestone 6).

`on_event()` reacts to `risk_alert` events: a critical one is escalated to a `supervisor_escalation` event. No dispatcher wires `on_event()` to anything yet (none exists anywhere in this framework) -- the event shape assumed here matches `agents.event_bus.events_since()`'s own return shape, the only shape this codebase defines for "an event" today.

**Safety invariant, unchanged from every other agent in this framework**: nothing in `agents/trading_supervisor/` closes a position, halts trading, or applies any change. `BaseAgent` has no `apply`/`execute` method; `TradingSupervisor` doesn't add one.

## Database schema

```sql
supervision_log          (Gate 7 decisions: candidate_name, symbol, decision, full report JSON)
agent_health_snapshots    (per-agent health per run_cycle(): is_stale, is_failing, snapshot JSON)
```

Both in `oi_history.db`, same file as every other agent table, indexed from the start (`idx_supervision_log_symbol_ts`, `idx_supervision_log_decision`, `idx_agent_health_snapshots_agent_ts`), `PRAGMA busy_timeout=5000` on every connection.

## Known, documented limitations

- **`SupervisionReport` is a parallel report shape to `agents.risk_manager.risk_report.RiskReport`**, not a shared base class. Retrofitting `RiskReport`'s already-tested shape into a shared abstraction was judged not worth the regression risk for this milestone; flagged as follow-up, consistent with the DRY finding from the pre-Milestone-6 architecture review.
- **Conflict detection only sees strategies promoted on or after this milestone** (needs the `direction` key `_parameter_payload` now stores). A strategy promoted under Milestone 5/6 alone has no direction on record and is conservatively excluded, never guessed at.
- **Event/expiry-risk calendars are caller-supplied, not looked up** -- this repo has no economic-calendar or per-symbol expiry-date data source (confirmed absent during Milestone 5/6 investigation too). `market_state.py` reports `"unknown"` honestly rather than fabricating a calendar.
- **No dispatcher exists to actually call `run_cycle()`/`on_event()` on a schedule or in reaction to real events** -- same gap `AUTONOMOUS_AGENTS_ARCHITECTURE.md`'s original P0 phase anticipated ("orchestrator.py") and which still doesn't exist. `run_cycle()` is directly callable today (and is what Gate 7 effectively runs a narrow slice of, per-candidate); a scheduled/triggered sweep across the whole system needs that orchestrator, out of scope for this milestone.

## Test summary

Roughly 90 new tests across `test_agents/trading_supervisor/` (market_state, agent_health, conflict_detector, data_health, supervision_engine, supervision_report, supervision_store, gate, supervisor_agent) plus extensions to `test_agents/test_audit_log.py` (`list_recent`), `test_agents/test_registry.py` (the snapshot/restore fix), and `test_agents/quant_researcher/test_research_engine.py` (the promotion test now asserts Gate 7 ran, ran last, and its verdict was persisted). Full repo suite: see the commit's own test run for the final count -- zero regressions from the pre-Milestone-7 baseline.
