"""
agents/quant_researcher/ -- Milestone 5: the AI Quant Researcher.

An autonomous research agent that discovers, backtests, statistically
validates, evolves, and (subject to the same five-gate pipeline every
other agent-authored change goes through) promotes trading strategies.
Mission statement: "using mathematical evidence instead of assumptions."

Module map (each one plug-in based, no hardcoded strategy logic):
  data_access.py           -- the ONLY place `import backtest` happens;
                               everything else here is testable with
                               synthetic DataFrames/dicts, never a real DB.
  features.py               -- FEATURE_REGISTRY: named, pure indicator/
                               formula functions (OI+Delta, VWAP+Gamma,
                               ATR+premium expansion, max pain, CPR,
                               IV crush, momentum, liquidity sweep, ...).
  hypotheses.py              -- HYPOTHESIS_CATALOG: declarative research
                               ideas (which features, what combinator) --
                               data, not code.
  strategy_spec.py            -- StrategySpec: the data object a
                               hypothesis becomes once given concrete
                               parameters. Still not code.
  strategy_runner.py           -- the ONE generic interpreter that turns
                               any StrategySpec + market data into trades.
  metrics.py                   -- turns trades into the required stat
                               set (Net Profit, Profit Factor, Win Rate,
                               Drawdown, Sharpe, Expectancy, Recovery
                               Factor, Trade Count).
  statistics_validation.py      -- rejects small-sample/insignificant
                               results before they can be promoted.
  evolution.py                  -- parameter optimisation, feature
                               selection, hypothesis combination --
                               every step recorded to Strategy Evolution
                               Memory.
  promotion.py                   -- objective candidate-vs-production-
                               baseline comparison; never a subjective
                               call.
  codegen.py                      -- materializes a promoted StrategySpec
                               into an actual, reviewable Python module
                               (template substitution only -- never
                               executes anything the research engine
                               produced).
  research_engine.py               -- orchestrates one full research
                               cycle end to end, wiring in
                               agents.memory (Market Regime / Trade
                               Journal / Institutional Pattern memory,
                               searched before every cycle) and routing
                               any promotion candidate through
                               agents.dev_agent's existing five gates.
"""
