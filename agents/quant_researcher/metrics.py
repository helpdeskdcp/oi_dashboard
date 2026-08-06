"""
agents/quant_researcher/metrics.py -- turns a StrategySpec's simulated
trades into the full required stat set: Net Profit, Profit Factor, Win
Rate, Drawdown, Sharpe Ratio, Expectancy, Recovery Factor, Trade Count.
Every field except Recovery Factor comes straight from
backtest.compute_advanced_trade_stats (via
agents.quant_researcher.data_access) -- the same definitions every other
engine in this repo already uses (SR, V3, Ichimoku, dynamic SR v4), so a
Quant Researcher result and a five-gate pipeline benchmark result are
never comparing two different definitions of "Sharpe Ratio". Recovery
Factor (net P&L / max drawdown) isn't computed there, so it's added here.
"""
from . import data_access


def compute_stats(trades: list) -> dict:
    stats = dict(data_access.compute_advanced_trade_stats(trades))
    net_pnl = stats.get("net_pnl") or 0.0
    max_dd = stats.get("max_drawdown") or 0.0
    stats["recovery_factor"] = round(net_pnl / max_dd, 3) if max_dd else None
    stats["trade_count"] = stats.get("total_trades", 0)
    return stats
