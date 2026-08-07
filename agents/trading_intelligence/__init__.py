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
                                             of the 3m archive into 15m/30m/
                                             1H/Daily (1m/5m honestly
                                             reported unavailable -- see
                                             that module's own docstring).
  paper_trading.py                             Module 5: virtual trade
                                                execution/tracking.
  ti_store.py                                     SQLite persistence:
                                                    ti_paper_trades,
                                                    ti_signal_log.
  api.py                                             Module 6: dashboard
                                                       support functions.
"""
