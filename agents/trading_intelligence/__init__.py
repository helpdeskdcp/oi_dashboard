"""
agents/trading_intelligence/ -- Milestone 10: BATI Trading Intelligence
Platform.

"This milestone focuses ONLY on trading intelligence, market analysis and
paper trading" -- built on top of, and reusing, the complete Version 1.0
autonomous framework (agents/runtime/, agents/risk_manager/, agents/memory/,
agents.sys_admin/) AND this repository's own pre-existing live option-chain
engine (oi_engine.py, greeks.py, market_structure.py, sr_probability_engine.py
-- the exact modules app.py's live dashboard and backtest.py both already
import, per that shared code's own "never duplicate this logic elsewhere"
rule). This package adds NOTHING competing with those -- every OI/bias/
signal computation here calls straight into them.

THE SINGLE MOST IMPORTANT DESIGN RULE IN THIS PACKAGE, read before touching
any file here: **no module in this package ever instantiates
app.AngelOneFetcher, ever imports SmartApi.SmartConnect, and ever touches
app._shared_angel_fetcher.** Every live-adjacent read goes through SQLite
(the `cycles`/`strikes`/`market_structure_snapshots` tables app.py's own
live loop already writes) or the `data/history/<symbol>/3m.*` candle
archives `history_engine.py` already writes -- the exact same "read what's
already been safely ingested, never open a second broker session" pattern
`agents/risk_manager/data_access.py` and `agents/quant_researcher/
data_access.py` established in Milestones 5-6. This project has a real,
documented incident of a test triggering a live duplicate Angel One login
by touching a route that held the shared session (see this repo's own
`/live-positions` landmine note) -- a second module doing the same thing
would be a second version of that exact incident, not a new kind of risk.
Verified structurally at the end of this milestone by an AST scan
(test_agents/trading_intelligence/test_safety.py), matching the same
verification style Milestone 9's own communication_contract.py check and
the Hardening Sprint's broker-touch scan both already established.

"Never place real orders. Recommendation mode and paper trading only." --
there is no function anywhere in this package that calls a broker order-
placement endpoint. Paper trades are pure SQLite INSERTs
(agents/trading_intelligence/ti_store.py), matching app.py's own
`db_open_paper_trade`'s "pure DB write, entry price supplied by the
caller, zero broker calls" pattern.

Modules:
  data_access.py             Read-only access to cycles/strikes/candles/
                              market_structure_snapshots -- the ONLY module
                              here that touches SQLite for market data.
  market_data.py                Module 1: OI/OI-change/PCR/IV/Greeks/VWAP/
                                 Volume, aggregated per symbol.
  institutional_intelligence.py    Module 2: Long/Short Build-up, Long
                                    Unwinding, Short Covering, OI Walls, Max
                                    Pain, Gamma Trap, Liquidity Sweep, Fake
                                    Breakout, Institutional Buying/Selling.
  strike_intelligence.py              Module 2b: per-strike breakdown (OI
                                       Wall/Support-Resistance/PCR/Build-up
                                       Type/Max Pain/Expected Move/AI Buy-
                                       Sell Probability/CE-PE Strength).
  ai_trading_engine.py                   Module 3: BUY CE / BUY PE / HOLD /
                                          NO TRADE with confidence/
                                          probability/risk score/entry/SL/
                                          target/reasoning.
  multi_timeframe.py                        Module 4: real local resampling
                                             of the 3m base into 15m/30m/1H/
                                             Daily; 1m/5m served by
                                             candle_recorder.py (see that
                                             module's own docstring).
  candle_recorder.py                        Milestone 20, Phase 6: real
                                             1m/3m/5m candles built
                                             in-process from LTP ticks
                                             app.py's own live loop already
                                             fetches -- zero new broker
                                             calls. Never touches
                                             AngelOneFetcher/SmartConnect;
                                             app.py calls INTO this module,
                                             never the other way around.
  paper_trading.py                             Module 5: virtual trade
                                                execution/tracking.
  ti_store.py                                     SQLite persistence:
                                                    ti_paper_trades,
                                                    ti_signal_log.
  api.py                                             Module 6: dashboard
                                                       support functions.
  structure_backtest.py                                Milestone 20, Phase 7:
                                                         real historical-
                                                         archive backtest for
                                                         detect_role_reversal()'s
                                                         own tunable
                                                         parameters -- fractal
                                                         pivot levels, no OI-
                                                         snapshot dependency.
  structure_tuning.py                                     Milestone 20, Phase 7:
                                                            the bounded/rate-
                                                            limited/audited
                                                            autonomous tuning
                                                            loop that acts on
                                                            structure_backtest.py's
                                                            results -- hard
                                                            parameter bounds,
                                                            minimum sample
                                                            size, minimum
                                                            improvement
                                                            margin, per-
                                                            parameter cooldown,
                                                            full audit log
                                                            (structure_tuning_log).
                                                            Gated by
                                                            config.
                                                            TI_ENABLE_STRUCTURE_TUNING
                                                            (default False).
  virtual_trailing.py                                     Milestone 21, Phase 1:
                                                            the Virtual
                                                            Trailing Engine --
                                                            paper-trade /
                                                            advisory-only
                                                            shadow layer that
                                                            tracks a dynamic
                                                            trailing SL/target
                                                            alongside each real
                                                            OPEN paper trade
                                                            (own table:
                                                            virtual_trailing_state).
                                                            Never calls
                                                            ti_store.
                                                            close_trade() or
                                                            touches a broker.
                                                            Gated by config.
                                                            TI_ENABLE_VIRTUAL_TRAILING
                                                            (default False).
  production_watchdog.py                                  Milestone 22:
                                                            six independent
                                                            read-only health
                                                            checks (scheduler
                                                            heartbeat, virtual
                                                            trailing cycle,
                                                            AI live snapshot,
                                                            DB round-trip,
                                                            Telegram retry
                                                            backlog, tracked-
                                                            trade count) on a
                                                            60s runtime-
                                                            scheduler cadence.
                                                            Escalates (report +
                                                            Telegram admin
                                                            alert) after 3
                                                            consecutive
                                                            failures of any ONE
                                                            check. No broker
                                                            calls, no order
                                                            APIs.
"""
