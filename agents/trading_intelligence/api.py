"""
agents/trading_intelligence/api.py -- Module 6: Dashboard support.
"Show: Live Option Chain, OI Analytics, Greeks, AI Signals, Risk,
Confidence, Paper PnL, Agent Health."

Plain, JSON-serializable read functions -- a Flask route (app.py's own
`/admin/trading-intelligence` page, added this milestone) can call these
without importing SQLite or any of this package's other modules directly,
the same "api.py is the one seam a route touches" pattern
agents.sys_admin.api and agents.risk_manager.api already established.
Agent Health is a live call into agents.sys_admin.api's own
get_agent_status() -- reused, not re-queried.
"""
import dataclasses
import datetime as dt

from .. import config
from ..sys_admin import api as sysadmin_api
from . import (
    ai_trading_engine,
    institutional_intelligence,
    market_data,
    multi_timeframe,
    paper_trading,
    strike_intelligence,
    ti_store,
)


def _asdict_findings(findings: list) -> list:
    return [dataclasses.asdict(f) for f in findings]


def get_symbol_overview(symbol: str, *, expiry_date: dt.date | None = None, capital: float | None = None,
                         risk_pct: float | None = None) -> dict:
    """One symbol's full picture: market data, institutional intelligence,
    the per-strike table, and the current AI recommendation."""
    snapshot = market_data.get_snapshot(symbol, expiry_date=expiry_date)
    if not snapshot.available:
        return {"symbol": symbol, "available": False, "reason": snapshot.reason}

    ii = institutional_intelligence.analyze(symbol, snapshot=snapshot, expiry_date=expiry_date)
    strikes_table = strike_intelligence.build_table(
        symbol, snapshot.strikes, underlying=snapshot.underlying_ltp, expiry_date=expiry_date,
    )
    recommendation = ai_trading_engine.evaluate(
        symbol, snapshot=snapshot, findings=ii.get("findings", []),
        capital=capital or config.TI_DEFAULT_CAPITAL, risk_pct=risk_pct or config.TI_DEFAULT_RISK_PCT,
        expiry_date=expiry_date,
    )
    timeframes = get_multi_timeframe_summary(symbol)

    return {
        "symbol": symbol, "available": True,
        "market_data": {
            "as_of_ts": snapshot.as_of_ts, "underlying_ltp": snapshot.underlying_ltp, "atm": snapshot.atm,
            "pcr": snapshot.pcr, "pcr_change": snapshot.pcr_change, "max_pain": snapshot.max_pain,
            "bias": snapshot.bias, "total_ce_oi": snapshot.total_ce_oi, "total_pe_oi": snapshot.total_pe_oi,
            "total_ce_oi_change": snapshot.total_ce_oi_change, "total_pe_oi_change": snapshot.total_pe_oi_change,
            "vwap": snapshot.vwap, "volume_today": snapshot.volume_today,
        },
        "institutional_intelligence": {
            "available": ii["available"], "findings": _asdict_findings(ii.get("findings", [])),
        },
        "strikes": [dataclasses.asdict(s) for s in strikes_table],
        "recommendation": dataclasses.asdict(recommendation),
        "timeframes": timeframes,
    }


def get_multi_timeframe_summary(symbol: str) -> dict:
    """Per-timeframe availability + latest bar only (not the full candle
    series -- a dashboard summary panel, not a chart-data endpoint)."""
    result = multi_timeframe.synchronize(symbol)
    summary = {}
    for tf, r in result.items():
        if r["available"]:
            last = r["candles"].iloc[-1]
            summary[tf] = {
                "available": True,
                "latest": {"datetime": str(last["datetime"]), "open": float(last["open"]),
                           "high": float(last["high"]), "low": float(last["low"]), "close": float(last["close"])},
                "bar_count": len(r["candles"]),
            }
        else:
            summary[tf] = {"available": False, "reason": r["reason"]}
    return summary


def get_paper_trading_summary(*, symbol: str | None = None) -> dict:
    stats = paper_trading.performance_stats(symbol=symbol)
    open_trades = ti_store.list_open_trades(symbol=symbol)
    closed_trades = ti_store.list_closed_trades(symbol=symbol, limit=20)
    return {"stats": stats, "open_trades": open_trades, "recent_closed_trades": closed_trades}


def run_scheduled_cycle(*, expiry_date: dt.date | None = None) -> dict:
    """One full autonomous cycle across every config.TI_WATCHED_SYMBOLS --
    what agents.runtime.agent_runtime's scheduled cycle (Milestone 9,
    wired in during the final review pass) calls unattended, market-hours
    gated the same way trading_supervisor's own cycle already is (see
    agents.runtime.scheduler._MARKET_SESSION_GATED_AGENTS).

    For each symbol: ONE snapshot -> ONE institutional-intelligence sweep
    -> ai_trading_engine.evaluate() (which already auto-closes any open
    paper trade whose target/SL was genuinely hit) -> paper_trading.
    enter_from_recommendation() if the recommendation is an actionable
    BUY. This is the exact same call sequence get_symbol_overview() makes
    for a manual dashboard load, with one addition: a manual load never
    opens a trade on its own (a human reads the recommendation first);
    this scheduled cycle does, because the whole point of an unattended
    cycle is to not need a human present to act on it. Still never
    touches the broker (see package __init__.py's own safety rule) --
    the only side effects are ti_paper_trades/ti_signal_log rows, exactly
    the same tables a human-triggered evaluate() call would write to."""
    results = {}
    for symbol in config.TI_WATCHED_SYMBOLS:
        snapshot = market_data.get_snapshot(symbol, expiry_date=expiry_date)
        if not snapshot.available:
            results[symbol] = {"available": False, "reason": snapshot.reason, "action": None, "trade_opened": False}
            continue
        ii = institutional_intelligence.analyze(symbol, snapshot=snapshot, expiry_date=expiry_date)
        rec = ai_trading_engine.evaluate(
            symbol, snapshot=snapshot, findings=ii.get("findings", []),
            capital=config.TI_DEFAULT_CAPITAL, risk_pct=config.TI_DEFAULT_RISK_PCT, expiry_date=expiry_date,
        )
        trade_id = paper_trading.enter_from_recommendation(rec) if rec.action in ("BUY CE", "BUY PE") else None
        results[symbol] = {
            "available": True, "action": rec.action, "trade_opened": trade_id is not None, "trade_id": trade_id,
        }
    return results


def get_overview(*, expiry_date: dt.date | None = None) -> dict:
    """The full Trading Intelligence Dashboard: every watched symbol's
    overview, paper trading performance, and Agent Health (reused from
    agents.sys_admin.api, not re-queried)."""
    return {
        "symbols": {
            symbol: get_symbol_overview(symbol, expiry_date=expiry_date) for symbol in config.TI_WATCHED_SYMBOLS
        },
        "paper_trading": get_paper_trading_summary(),
        "agent_health": sysadmin_api.get_agent_status(),
    }
