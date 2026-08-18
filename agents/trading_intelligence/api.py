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
import logging

from .. import config
from ..sys_admin import api as sysadmin_api
from . import (
    ai_trading_engine,
    execution_state,
    institutional_intelligence,
    market_data,
    multi_timeframe,
    paper_trading,
    strike_intelligence,
    telegram_notifier,
    ti_store,
)

log = logging.getLogger(__name__)

# Post-launch upgrade, Phase 2: the LangGraph shadow-signal layer is an
# optional dependency at the IMPORT boundary, not just at the call site.
# A plain top-level `from . import signal_graph` would mean any failure
# to import langgraph itself (missing package, broken/partial install,
# a future dependency conflict) takes down this ENTIRE module -- and
# therefore app.py's `from agents.trading_intelligence import api as
# ti_api`, i.e. the whole trading-intelligence dashboard and scheduled
# cycle -- regardless of whether TI_ENABLE_SIGNAL_GRAPH_SHADOW is even
# on. That would violate this package's own "advisory layer failure
# must never affect the real engine" contract at a point no runtime
# try/except can reach (the crash happens before any of this module's
# code ever runs). Caught here instead: signal_graph is None when
# unavailable, and the one gated call site below checks for that before
# ever touching it.
try:
    from . import signal_graph
except Exception as e:  # pragma: no cover -- exercised by test_signal_graph_import_isolation.py
    signal_graph = None
    log.warning(f"signal_graph shadow layer unavailable (import failed, real engine unaffected): {e}")


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


def run_scheduled_cycle(*, expiry_date: dt.date | None = None, expiry_dates: dict | None = None,
                         symbols=None) -> dict:
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
    the same tables a human-triggered evaluate() call would write to.

    `expiry_dates` (Milestone 17+): {symbol: date} for a PER-symbol expiry
    -- every real caller watches a mix of NSE indexes and MCX commodities
    with genuinely different expiry calendars, so one shared `expiry_date`
    for all of them was always wrong (that's why every real call site
    before this milestone left `expiry_date` at its None default --
    Time Horizon/expiry-day weighting were silently unavailable for
    EVERY symbol, always). `expiry_date` alone still works as a
    single-value fallback for a symbol missing from `expiry_dates` (or
    when `expiry_dates` isn't given at all), so existing callers/tests are
    unaffected.

    `symbols` (Milestone 19+): overrides which symbols this ONE call
    processes -- defaults to every config.TI_WATCHED_SYMBOLS symbol,
    exactly the prior behavior, so every existing caller is unaffected.
    agents.runtime.agent_runtime's own scheduled cycle now passes only
    the currently-exchange-open subset (agents.runtime.market_session.
    active_symbols()) so an NSE symbol's stale post-close cycle data
    never gets evaluated during MCX-only hours, and vice versa."""
    results = {}
    for symbol in (symbols if symbols is not None else config.TI_WATCHED_SYMBOLS):
        symbol_expiry = (expiry_dates or {}).get(symbol, expiry_date)
        snapshot = market_data.get_snapshot(symbol, expiry_date=symbol_expiry)
        if not snapshot.available:
            results[symbol] = {"available": False, "reason": snapshot.reason, "action": None, "trade_opened": False}
            continue
        ii = institutional_intelligence.analyze(symbol, snapshot=snapshot, expiry_date=symbol_expiry)
        rec = ai_trading_engine.evaluate(
            symbol, snapshot=snapshot, findings=ii.get("findings", []),
            capital=config.TI_DEFAULT_CAPITAL, risk_pct=config.TI_DEFAULT_RISK_PCT, expiry_date=symbol_expiry,
        )
        trade_id = (
            paper_trading.enter_from_recommendation(rec, snapshot=snapshot, findings=ii.get("findings", []))
            if rec.action in ("BUY CE", "BUY PE") else None
        )
        # Post-launch upgrade, Phase B1: execution_state shadow observation
        # -- advisory/persisted-only, gated off by default (config.
        # TI_ENABLE_EXECUTION_STATE_SHADOW). Fires exactly when a real
        # (paper) position was actually opened this cycle -- the same
        # condition trade_id is not None already reports -- reusing
        # trade_id (ti_paper_trades' own primary key) as the
        # execution_id rather than inventing a second identity scheme.
        # execution_state.py makes no broker call and has no optional
        # dependency (see its own module docstring), so unlike
        # signal_graph above this needs no import-time guard -- only a
        # runtime try/except, so a bug in this wiring can never affect
        # the real cycle above, which has already fully completed
        # (paper-trade entry) by this point.
        if config.TI_ENABLE_EXECUTION_STATE_SHADOW and trade_id is not None:
            try:
                execution_id = f"paper_trade_{trade_id}"
                execution_state.create_execution(
                    execution_id, instrument=symbol, direction=rec.direction, strike=rec.strike,
                    entry_price=rec.entry_price, quantity=rec.qty, sl=rec.sl_price,
                    t1=rec.targets[0] if rec.targets else rec.target_price,
                    t2=rec.targets[1] if len(rec.targets) > 1 else None,
                    t3=rec.targets[2] if len(rec.targets) > 2 else None,
                    confidence=rec.confidence, decision_reason=rec.reasoning,
                    signal_reference=f"ti_paper_trades:{trade_id}",
                )
                execution_state.transition(
                    execution_id, "APPROVED",
                    reason="risk gate, position sizing, and market-session checks already passed -- paper trade opened",
                )
            except Exception as e:
                log.warning(f"execution_state shadow wiring failed for {symbol!r}: {e}")
        # Milestone 19: Telegram signal broadcast -- ONLY source is this
        # engine's own actionable Recommendation, never the S/R Engine
        # (see telegram_notifier.py's own module docstring for why that
        # pipeline was disconnected). Gated here, not inside the notifier
        # itself, so the notifier stays a pure format-and-send utility --
        # same "orchestration decides, delivery just delivers" split
        # paper_trading.enter_from_recommendation()'s own call above
        # already establishes.
        if rec.action in ("BUY CE", "BUY PE") and (rec.confidence or 0) >= config.TI_TELEGRAM_MIN_CONFIDENCE:
            telegram_notifier.send_trading_intelligence_signal(_build_telegram_payload(rec))
        # Post-launch upgrade, Phase 2: LangGraph shadow-signal layer --
        # advisory/observation only, gated off by default (config.
        # TI_ENABLE_SIGNAL_GRAPH_SHADOW). Reuses this cycle's already-
        # computed snapshot/findings/rec (no second snapshot or
        # institutional-sweep read); run_shadow() itself never raises,
        # but this call site is wrapped too so a bug in this wiring code
        # can never affect the real cycle above, which has already fully
        # completed (paper-trade entry + Telegram) by this point.
        # `signal_graph is not None` (module import-time guard above)
        # covers the langgraph-dependency-unavailable case, which a
        # runtime try/except here cannot reach.
        if config.TI_ENABLE_SIGNAL_GRAPH_SHADOW and signal_graph is not None:
            try:
                signal_graph.run_shadow(
                    symbol, snapshot=snapshot, findings=ii.get("findings", []), recommendation=rec,
                    expiry_date=symbol_expiry, real_engine_action=rec.action,
                )
            except Exception as e:
                log.warning(f"signal_graph shadow call site failed for {symbol!r}: {e}")
        results[symbol] = {
            "available": True, "action": rec.action, "trade_opened": trade_id is not None, "trade_id": trade_id,
        }
    return results


def _build_telegram_payload(rec: "ai_trading_engine.Recommendation") -> dict:
    """Maps a real Recommendation onto telegram_notifier's payload shape.
    Deliberately does NOT invent institutional_score/premium_momentum/
    oi_structure/vwap_structure/repeated_rejection -- this engine never
    computes those as discrete fields (see telegram_notifier.py's own
    docstring); the genuine equivalent this engine DOES produce is the
    four free-text reasoning strings below, passed through as
    reasoning_details rather than faked into a category label."""
    return {
        "symbol": rec.symbol,
        "signal_type": rec.action.replace(" ", "_"),  # "BUY CE" -> "BUY_CE"
        "overall_bias": rec.market_bias,
        "confidence": rec.confidence,
        "entry_zone": {"strike": rec.strike, "price": rec.entry_price},
        "targets": rec.targets,
        "stop_loss": rec.sl_price,
        "reasoning": rec.reasoning,
        "reasoning_details": [
            d for d in (rec.institutional_reasoning, rec.oi_reasoning, rec.greeks_reasoning, rec.price_action_reasoning) if d
        ],
    }


def get_overview(*, expiry_date: dt.date | None = None, expiry_dates: dict | None = None) -> dict:
    """The full Trading Intelligence Dashboard: every watched symbol's
    overview, paper trading performance, and Agent Health (reused from
    agents.sys_admin.api, not re-queried).

    `expiry_dates`: see run_scheduled_cycle()'s own docstring for why this
    is per-symbol, not one shared date, and why `expiry_date` alone
    remains a valid fallback."""
    return {
        "symbols": {
            symbol: get_symbol_overview(symbol, expiry_date=(expiry_dates or {}).get(symbol, expiry_date))
            for symbol in config.TI_WATCHED_SYMBOLS
        },
        "paper_trading": get_paper_trading_summary(),
        "agent_health": sysadmin_api.get_agent_status(),
    }
