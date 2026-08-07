"""
agents/trading_intelligence/paper_trading.py -- Module 5: Paper Trading.
"Execute only virtual trades. Track: Win Rate, Profit Factor, Drawdown,
Expectancy, Trade Journal. Store every result inside BATI Memory."

"Execute" here means exactly one thing: a pure SQLite INSERT into
ti_store.ti_paper_trades, with every price supplied by the caller (from
an already-computed ai_trading_engine.Recommendation) -- there is no
function anywhere in this module, or this package, that calls a broker
order-placement endpoint. See package __init__.py's own safety rule and
test_agents/trading_intelligence/test_safety.py's structural verification
of that fact.

Win Rate/Profit Factor/Drawdown/Expectancy: reuses
backtest.compute_advanced_trade_stats() -- the SAME statistics function
every other backtest/paper-trading surface in this repository already
uses, operating on the same plain {"points", "exit_reason", ...} trade-
dict shape it already expects. Never a second implementation of these
definitions.

Trade Journal: reuses agents.memory's existing agent_memory_trade_journal
table (Milestone 4, built with exactly this in mind: "Entry/Exit/
Screenshot/AI Reason/Actual Result/Learning") -- not a second, competing
journal table.

Milestone 11, Module 11.3: enter_from_recommendation() is also the ONE
place this engine captures the trade's entry-time reasoning context
(regime_profile.classify(), timeframe_confirmation.check(),
trade_quality.institutional_backing()) for trade_quality.score() to read
back once the trade closes -- see trade_quality.py's own module docstring
for why this must happen live, at entry, rather than being recomputed
retroactively.
"""
from . import regime_profile, ti_store, timeframe_confirmation, trade_quality
from .ai_trading_engine import Recommendation


def enter_from_recommendation(recommendation: Recommendation, *, snapshot=None, findings: list | None = None) -> int | None:
    """Opens a new ti_paper_trades row from a "BUY CE"/"BUY PE"
    Recommendation. Returns the new trade id, or None if `recommendation`
    isn't an actionable entry (NO_TRADE/HOLD -- nothing to open) or sizes
    to zero quantity (the risk budget couldn't accommodate this stop --
    see position_sizing.compute_quantity's own docstring: sizing to 0 is
    a deliberate "skip the trade," not a bug to work around).

    `snapshot`/`findings`: pass these when the caller (api.run_scheduled_cycle(),
    the normal path) already computed them this cycle for evaluate() --
    avoids a second institutional-intelligence sweep for the same data,
    the same dedup discipline evaluate() itself already uses. Standalone
    callers (every test here) leave these None and trade_quality.
    institutional_backing() fetches fresh, same as evaluate() would."""
    if recommendation.action not in ("BUY CE", "BUY PE"):
        return None
    if not recommendation.qty:
        return None

    regime = regime_profile.classify(recommendation.symbol, snapshot=snapshot)
    alignment = timeframe_confirmation.check(recommendation.symbol, direction=recommendation.direction)
    backed = trade_quality.institutional_backing(
        recommendation.symbol, direction=recommendation.direction, strike=recommendation.strike,
        snapshot=snapshot, findings=findings,
    )

    return ti_store.open_trade(
        symbol=recommendation.symbol, strike=recommendation.strike, direction=recommendation.direction,
        entry_price=recommendation.entry_price, target_price=recommendation.target_price,
        sl_price=recommendation.sl_price, qty=recommendation.qty, confidence=recommendation.confidence,
        probability=recommendation.probability, risk_score=recommendation.risk_score,
        reasoning=recommendation.reasoning,
        regime_trend_at_entry=regime.trend_regime, regime_volatility_at_entry=regime.volatility_regime,
        timeframe_alignment_score_at_entry=alignment.alignment_score,
        institutional_backed_at_entry=backed,
    )


def record_journal_entry(trade: dict, *, memory_store, learning: str | None = None) -> int:
    """Writes one CLOSED trade into agents.memory's trade journal --
    called once, right after close_trade(), never for an OPEN trade
    (nothing to journal about an outcome that hasn't happened yet)."""
    return memory_store.record_trade_journal(
        symbol=trade["symbol"], entry_price=trade["entry_price"], exit_price=trade["exit_price"],
        entry_time=trade["entry_time"], exit_time=trade["exit_time"],
        ai_reason=trade.get("reasoning"),
        actual_result=f"{trade['exit_reason']}: {trade['points']:+.2f} pts" if trade.get("points") is not None else None,
        learning=learning,
    )


def performance_stats(*, symbol: str | None = None) -> dict:
    """Win Rate/Profit Factor/Drawdown/Expectancy across every CLOSED
    trade this engine has made -- reuses backtest.compute_advanced_trade_stats
    unchanged."""
    import backtest
    trades = ti_store.list_closed_trades(symbol=symbol, limit=10_000)
    return backtest.compute_advanced_trade_stats(trades)


def close_and_journal(trade_id: int, *, exit_price: float, exit_reason: str, memory_store, learning: str | None = None) -> dict:
    """Closes a trade AND journals it in one call -- the normal path a
    scheduled cycle uses; close_trade()/record_journal_entry() stay
    separately callable for tests and for a caller that wants to close
    now and journal later (e.g. after a human adds a screenshot)."""
    trade = ti_store.close_trade(trade_id, exit_price=exit_price, exit_reason=exit_reason)
    record_journal_entry(trade, memory_store=memory_store, learning=learning)
    return trade
