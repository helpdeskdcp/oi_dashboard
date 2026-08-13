"""
agents/trading_intelligence/paper_trade_diagnostics.py -- Milestone 20,
Phase 6: a read-only "why did today's paper trades win or lose" report,
backing GET /api/papertrades/diagnostics. Pure read + arithmetic over
ti_store.list_closed_trades_for_date() -- never writes, never touches a
broker, never changes a single trade's outcome.

Every field here is either a plain aggregate (win_rate, average_win,
average_loss, expectancy, biggest_loser) or reuses an ALREADY-ESTABLISHED
threshold from elsewhere in this package, documented below rather than
guessed fresh for this report:

- against_structure: timeframe_confirmation.py's own "CONTRARY" tier
  (alignment_score <= ALIGNMENT_CONTRARY_PERCENTILE, i.e. roughly 1-of-3
  or fewer confirming timeframes) -- the trade's own multi-timeframe
  read actively disagreed with its direction at entry.
- low_confidence_trades: ai_trading_engine.CALIBRATION_BUCKETS[0]'s own
  (0, 39) bucket -- the same "low confidence" boundary the engine's own
  calibration report already uses, not a new number invented for this
  report.
- oversized_stoploss_trades: OVERSIZED_STOPLOSS_RISK_SCORE_MIN below --
  a NEW, explicitly chosen (not calibrated) cutoff on the trade's own
  stored risk_score (see ai_trading_engine._compute_risk_score()'s own
  docstring for what that 0-100 score means). Documented as chosen,
  not derived, so a future calibration pass can replace it honestly.
"""
import statistics

from . import ti_store

LOW_CONFIDENCE_MAX = 39   # ai_trading_engine.CALIBRATION_BUCKETS[0] == (0, 39)
CONTRARY_ALIGNMENT_MAX = 100 / 3   # timeframe_confirmation.ALIGNMENT_CONTRARY_PERCENTILE
OVERSIZED_STOPLOSS_RISK_SCORE_MIN = 70   # explicitly chosen, not calibrated -- see module docstring


def compute_diagnostics(date_str: str) -> dict:
    """`date_str`: "YYYY-MM-DD". Returns available=False with a reason
    when there are no closed trades that day -- never a fabricated 0%
    win rate standing in for "nothing happened."."""
    trades = ti_store.list_closed_trades_for_date(date_str)
    if not trades:
        return {"date": date_str, "available": False, "reason": "no closed trades for this date", "trade_count": 0}

    points = [t["points"] if t["points"] is not None else 0.0 for t in trades]
    wins = [p for p in points if p > 0]
    losses = [p for p in points if p <= 0]

    biggest_loser_trade = min(trades, key=lambda t: (t["points"] if t["points"] is not None else 0.0))

    against_structure = sum(
        1 for t in trades
        if t.get("timeframe_alignment_score_at_entry") is not None
        and t["timeframe_alignment_score_at_entry"] <= CONTRARY_ALIGNMENT_MAX
    )
    low_confidence_trades = sum(
        1 for t in trades if t.get("confidence") is not None and t["confidence"] <= LOW_CONFIDENCE_MAX
    )
    oversized_stoploss_trades = sum(
        1 for t in trades if t.get("risk_score") is not None and t["risk_score"] >= OVERSIZED_STOPLOSS_RISK_SCORE_MIN
    )

    return {
        "date": date_str,
        "available": True,
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "average_win": round(statistics.mean(wins), 2) if wins else None,
        "average_loss": round(statistics.mean(losses), 2) if losses else None,
        "expectancy": round(statistics.mean(points), 2),
        "biggest_loser": f"{biggest_loser_trade['symbol']} {biggest_loser_trade['direction']}",
        "biggest_loser_points": biggest_loser_trade["points"],
        "against_structure": against_structure,
        "low_confidence_trades": low_confidence_trades,
        "oversized_stoploss_trades": oversized_stoploss_trades,
    }
