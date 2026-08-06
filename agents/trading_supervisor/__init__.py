"""
agents/trading_supervisor/ -- Milestone 7: the AI Trading Supervisor.

"Oversee all other BATI agents and the live trading workflow." Where
Milestones 3-6 each own one slice of the pipeline (detect+patch, memory,
research, risk), this agent is the cross-cutting layer above all of
them: it re-verifies what they produced, watches whether they're
themselves behaving normally, and is the first agent in this framework
to actually subclass agents.base_agent.BaseAgent and register via
agents.registry (every prior concrete agent shipped as a free-standing
function module instead -- a drift the pre-Milestone-6 architecture
review flagged; this milestone is where it stops, not where it deepens).

Module map:
  market_state.py       -- trend/range/volatility/expiry/event-risk
                          classification. Reuses REAL, already-computed
                          data wherever it exists (backtest.
                          load_market_structure_snapshots for trend/
                          range regime, INDIA VIX's own candle archive
                          for volatility) rather than a second,
                          possibly-disagreeing classifier. Expiry/event
                          risk have no calendar data source in this
                          repo -- both honestly report "unknown" rather
                          than guessing, unless a caller supplies one.
  agent_health.py         -- "Monitor all AI agents (Developer, Memory,
                          Quant Researcher, Risk Manager)": recent
                          activity/outcome mix per agent (via
                          agents.audit_log.list_recent) plus a Memory
                          freshness/reachability check.
  conflict_detector.py     -- "Detect conflicting signals between
                          strategies": same-symbol, opposite-direction
                          currently-promoted strategies.
  data_health.py             -- "Detect abnormal behaviour, stale data,
                          API failures, inconsistent market feeds.
                          Monitor broker connectivity." Every signal
                          here is an indirect proxy (data staleness,
                          audit-log failure clustering) -- this module
                          NEVER calls the live Angel One session, the
                          same landmine agents.risk_manager.data_access
                          already documented and avoided.
  supervision_engine.py       -- verify(): combines market state +
                          conflicts + health + "did the risk gate
                          actually run and pass" into one explainable
                          SupervisionVerdict. "Trigger alerts instead of
                          automatic execution when uncertainty is high"
                          means unresolved uncertainty always lands on
                          REQUIRES_REVIEW, never a silent APPROVED.
  supervision_report.py        -- shared JSON + human-readable report
                          shape, same pattern as agents.risk_manager.
                          risk_report.
  supervision_store.py          -- SQLite persistence (supervision_log,
                          agent_health_snapshots), indexed from the
                          start.
  gate.py                        -- wraps a SupervisionVerdict into a
                          agents.dev_agent.gates.base.GateResult --
                          Gate 7 of the promotion pipeline, appended
                          after agents.risk_manager.gate (Gate 6).
  supervisor_agent.py             -- TradingSupervisor(BaseAgent):
                          run_cycle() sweeps every agent + market +
                          data health for Findings; on_event() reacts
                          to risk_alert events agents.risk_manager.
                          portfolio_monitor already publishes.
"""
