"""
agents/risk_manager/ -- Milestone 6: the AI Risk Manager.

"Protect capital before profit. Prevent bad AI-generated strategies from
reaching production. Continuously monitor live portfolio risk. Keep all
risk decisions fully explainable and auditable."

Module map:
  risk_engine.py        -- the Promotion Risk Gate: position sizing,
                          capital allocation, exposure limits (symbol/
                          sector/strategy), concurrent-trade limits,
                          correlation analysis, VaR, CVaR (Expected
                          Shortfall), Monte Carlo drawdown simulation,
                          stress testing, and a composite 0-100 risk
                          score. evaluate_promotion() is what
                          agents/risk_manager/gate.py wraps into Gate 6
                          of the pipeline.
  gate.py                -- turns evaluate_promotion()'s RiskAssessment
                          into a agents.dev_agent.gates.base.GateResult,
                          so it composes with the SAME approval_engine
                          every other gate already goes through.
  risk_intelligence.py     -- AI Risk Intelligence: searches
                          agents.memory for prior failures before every
                          assessment, detects repeated failure patterns,
                          recommends safer parameters, and refuses to
                          silently repeat a known-bad configuration
                          without explaining why this attempt differs.
  portfolio_monitor.py      -- the Live Portfolio Risk Monitor: reads
                          real (paper-trading) position/wallet data,
                          computes exposure/heat/margin/drawdown/
                          concentration/correlation/Greeks, publishes
                          alerts via agents.event_bus (its first real
                          producer), and generates emergency
                          RECOMMENDATIONS -- never an automatic action;
                          this module has no method that closes a
                          position or halts trading, matching every
                          other agent's propose-only posture.
  risk_report.py             -- RiskReport: one shared, explainable
                          report shape (JSON + human-readable) used by
                          both the promotion gate and the live monitor.
  risk_store.py                -- SQLite persistence (risk_assessments,
                          risk_alerts, risk_snapshots) in the same
                          oi_history.db every other agent module uses,
                          same busy_timeout/index conventions the
                          architecture review established.
  api.py                         -- the Risk API: plain, JSON-serializable
                          read functions a Flask route (or anything
                          else) can call without importing SQLite
                          directly.
"""
