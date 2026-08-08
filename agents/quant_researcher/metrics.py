"""
agents/quant_researcher/metrics.py -- turns a StrategySpec's simulated
trades into the full required stat set: Net Profit, Profit Factor, Win
Rate, Drawdown, Sharpe Ratio, Expectancy, Recovery Factor, Trade Count,
Sortino Ratio, Equity Curve. Every field except Recovery Factor, Sortino
Ratio, and Equity Curve comes straight from
backtest.compute_advanced_trade_stats (via
agents.quant_researcher.data_access) -- the same definitions every other
engine in this repo already uses (SR, V3, Ichimoku, dynamic SR v4), so a
Quant Researcher result and a five-gate pipeline benchmark result are
never comparing two different definitions of "Sharpe Ratio". Recovery
Factor (net P&L / max drawdown) isn't computed there, so it's added here.

Milestone 11, Module 11.6: Sortino Ratio and Equity Curve are added here
too -- purely additive new dict keys; every existing key's value is
unchanged. See agents.trading_intelligence.paper_trading.performance_stats(),
switched in this same module to route through compute_stats() rather
than calling backtest.compute_advanced_trade_stats() directly, so it
gets both new fields automatically with zero duplicate math.

NOT the only "sortino_ratio" definition in this repository: agents.
dev_agent.regression_analyzer.enrich_stats() independently computes a
Sortino Ratio under the same dict key, for a different purpose (AI
Developer regression gating) and with different math (stdev of only the
losing points around their own mean, vs. this module's RMS-from-zero
semi-deviation over every trade). The two never feed into each other
today (enrich_stats() is never called with this function's output), but
they ARE two different numbers under one name -- flagged explicitly here
rather than silently claimed as unified, so a future integration doesn't
assume it can freely mix the two.
"""
import statistics

from . import data_access


def _sortino_ratio(points: list) -> float | None:
    """Mean per-trade points / downside deviation -- the semi-deviation
    of returns below zero (target=0, the same "no calendar annualization"
    convention backtest.compute_advanced_trade_stats's own Sharpe Ratio
    already uses: this is a per-trade points ratio, not a time-resampled
    one). None (undefined) when there's no downside variance to divide
    by -- the exact same honest-degradation gate Sharpe Ratio already
    applies (there, `stdev > 0`; here, `downside_deviation > 0`), never a
    fabricated number standing in for "no losing trades yet."."""
    if len(points) < 2:
        return None
    downside_deviation = statistics.mean(min(p, 0.0) ** 2 for p in points) ** 0.5
    if downside_deviation == 0:
        return None
    return round(statistics.mean(points) / downside_deviation, 3)


def _equity_curve(points: list) -> list:
    """Cumulative running P&L after each trade, in trade order -- the
    same equity accumulation compute_advanced_trade_stats's own
    max_drawdown computation already walks internally (equity += p, per
    trade), exposed here as the full time series rather than collapsed
    into a single max-drawdown number."""
    equity_curve = []
    equity = 0.0
    for p in points:
        equity += p
        equity_curve.append(round(equity, 2))
    return equity_curve


def compute_stats(trades: list) -> dict:
    stats = dict(data_access.compute_advanced_trade_stats(trades))
    net_pnl = stats.get("net_pnl") or 0.0
    max_dd = stats.get("max_drawdown") or 0.0
    stats["recovery_factor"] = round(net_pnl / max_dd, 3) if max_dd else None
    stats["trade_count"] = stats.get("total_trades", 0)

    points = [t.get("points") or 0.0 for t in trades]
    stats["sortino_ratio"] = _sortino_ratio(points)
    stats["equity_curve"] = _equity_curve(points)
    return stats
