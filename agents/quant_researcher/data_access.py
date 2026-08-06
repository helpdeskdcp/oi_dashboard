"""
agents/quant_researcher/data_access.py -- the ONLY module in
agents/quant_researcher/ that imports the real `backtest` module (repo
root). Every other module here takes plain pandas DataFrames / lists of
dicts, so it can be unit-tested with synthetic data and never needs a
real oi_history.db or data/history/ archive -- the same reason
agents/dev_agent/gates/backtest_compare.py shells `import backtest` out
to a subprocess instead of importing it directly into the agent process
(there it's to avoid a sys.modules collision between two worktree copies;
here it's simpler -- just one process, one import -- but the isolation
principle, "keep the heavy production import at one narrow seam," is the
same). Imported lazily (inside each function, not at module top) so
merely importing agents.quant_researcher never pulls in backtest.py's
full dependency chain until a caller actually asks for data.
"""
import dataclasses


def load_candles(symbol: str, *, timeframe: str = "3m"):
    """Returns the datetime-sorted OHLCV DataFrame for `symbol` (or an
    empty one if no archive exists -- see backtest.load_intraday_candles,
    which already degrades gracefully rather than raising)."""
    import backtest
    return backtest.load_intraday_candles(symbol, timeframe=timeframe)


def load_cycles_for_range(symbol: str, date_from: str, date_to: str) -> list:
    """Returns option-chain cycles for `symbol` between date_from/date_to
    (inclusive), normalized into plain dicts -- see normalize_cycles()
    below -- so agents/quant_researcher/features.py never needs to import
    backtest.StrikeRow or know it exists. Empty list if there's no
    oi_history.db data for this symbol/range yet (a brand-new symbol, or
    a range before logging started)."""
    import backtest
    return normalize_cycles(backtest.load_cycles(symbol, date_from, date_to))


def normalize_cycles(raw_cycles: list) -> list:
    """Converts backtest.load_cycles()'s [{"cycle": dict, "rows": [StrikeRow,...]}]
    shape into [{**cycle_fields, "strikes": [plain dict, ...]}] -- flat,
    dependency-free dicts that every feature function in features.py is
    written against."""
    normalized = []
    for entry in raw_cycles or []:
        cycle = dict(entry.get("cycle") or {})
        rows = entry.get("rows") or []
        cycle["strikes"] = [
            dataclasses.asdict(r) if dataclasses.is_dataclass(r) else dict(r)
            for r in rows
        ]
        normalized.append(cycle)
    return normalized


def compute_advanced_trade_stats(trades: list) -> dict:
    """Delegates to backtest.compute_advanced_trade_stats -- the single
    source of truth for Net Profit / Profit Factor / Win Rate / Drawdown /
    Expectancy / Sharpe Ratio definitions, shared with every other engine
    in this repo (SR, V3, Ichimoku, dynamic SR v4) rather than a second,
    possibly-drifting copy of the same math."""
    import backtest
    return backtest.compute_advanced_trade_stats(trades)


def production_baseline_stats(symbol: str, date_from: str, date_to: str) -> dict:
    """The "current production strategy" a candidate must outperform:
    the same dynamic SR v4 engine agents/dev_agent/gates/backtest_compare.py
    already treats as the regression baseline, replayed over the
    requested window. Real, not synthetic -- this is what's actually
    running in production today."""
    import backtest
    trades, _cycle_count, _meta = backtest.simulate_dynamic_sr_v4_trades(symbol, date_from, date_to)
    return backtest.compute_advanced_trade_stats(trades)
