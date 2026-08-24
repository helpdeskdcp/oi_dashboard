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
import concurrent.futures
import dataclasses
import datetime as dt
import logging
import threading
import time

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


def _advance_execution_states(execution_id: str, states: tuple, *, reason: str) -> None:
    """Post-launch upgrade, Phase B2: chains execution_state.transition()
    through a sequence of states in order, e.g. (READY, ORDER_INTENT,
    SUBMITTED, FILLED, MONITORING) -- paper mode has no distinct broker
    order/exit lifecycle, so these stages are reached back-to-back rather
    than at genuinely separate points in time. Stops (without raising) at
    the first rejected step -- transition() itself never raises, so this
    just avoids continuing to call it against a state graph that already
    didn't accept the previous hop."""
    for state in states:
        result = execution_state.transition(execution_id, state, reason=reason)
        if not result["ok"]:
            log.warning(f"execution_state shadow: {execution_id} could not advance to {state!r}: {result['reason']}")
            break


def _reconcile_execution_state_exits(symbol: str, open_trades_before: list) -> None:
    """Post-launch upgrade, Phase B2 (reconciliation fix): detects, via
    the open_trades-before/after diff, which trade_id(s) for `symbol`
    closed DURING the caller's own ai_trading_engine.evaluate() call
    (that function's existing auto-close-on-target/SL-hit logic --
    never re-implemented here), then advances that execution straight
    through EXIT_INTENT -> EXIT -> COMPLETED.

    CRITICAL: evaluate() can close an open trade from EVERY caller that
    reaches it, not only run_scheduled_cycle() -- get_symbol_overview()
    (a plain dashboard read, fired by every /api/trading-intelligence/
    overview poll, including the client's own 15s auto-refresh) calls
    evaluate() too, and its own target/SL check can close a trade there
    just as easily. A real trade (paper_trade_75, 2026-08-18) was found
    CLOSED in ti_paper_trades while its execution_state row stayed
    stuck at MONITORING, because only run_scheduled_cycle() called this
    reconciliation -- the trade had actually closed during an ordinary
    dashboard read a few cycles earlier. Both call sites now share this
    exact same function so this can never diverge into two copies of
    the same logic again.

    A trade_id with no matching execution_state row (shadow was off
    when it opened) is a graceful, already-handled no-op --
    transition() logs and rejects an unknown execution_id rather than
    raising. Callers wrap this in their own try/except is unnecessary
    -- this function catches its own failures so a bug here can never
    affect the real read/cycle that called it."""
    try:
        still_open_ids = {t["id"] for t in ti_store.list_open_trades(symbol=symbol)}
        closed_ids = {t["id"] for t in open_trades_before} - still_open_ids
        if not closed_ids:
            return
        closed_by_id = {t["id"]: t for t in ti_store.list_closed_trades(symbol=symbol, limit=len(closed_ids) + 5)}
        for closed_id in closed_ids:
            closed = closed_by_id.get(closed_id)
            if closed is None:
                continue
            execution_id = f"paper_trade_{closed_id}"
            _advance_execution_states(
                execution_id, ("EXIT_INTENT",),
                reason=f"paper trade closed: {closed['exit_reason']} at {closed['exit_price']} "
                       f"({closed['points']:+.2f} pts)",
            )
            _advance_execution_states(
                execution_id, ("EXIT", "COMPLETED"),
                reason="paper mode -- no distinct broker exit lifecycle, advanced straight to COMPLETED",
            )
    except Exception as e:
        log.warning(f"execution_state shadow exit-side reconciliation failed for {symbol!r}: {e}")


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
    # Post-launch upgrade, Phase B2 (reconciliation fix): captured BEFORE
    # evaluate() -- which, exactly like run_scheduled_cycle()'s own call,
    # can close an already-open trade internally via its own target/SL
    # check. This function is a plain dashboard read (fired by every
    # /api/trading-intelligence/overview poll, including the client's own
    # 15s auto-refresh) -- without this, a trade that happens to close
    # during a READ rather than a scheduled cycle would update
    # ti_paper_trades correctly but leave its execution_state row stuck
    # at MONITORING forever. See _reconcile_execution_state_exits()'s own
    # docstring for the real trade (paper_trade_75) this was found on.
    open_trades_before = ti_store.list_open_trades(symbol=symbol) if config.TI_ENABLE_EXECUTION_STATE_SHADOW else []
    recommendation = ai_trading_engine.evaluate(
        symbol, snapshot=snapshot, findings=ii.get("findings", []),
        capital=capital or config.TI_DEFAULT_CAPITAL, risk_pct=risk_pct or config.TI_DEFAULT_RISK_PCT,
        expiry_date=expiry_date,
    )
    if config.TI_ENABLE_EXECUTION_STATE_SHADOW and open_trades_before:
        _reconcile_execution_state_exits(symbol, open_trades_before)
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
        # Post-launch upgrade, Phase B2: captured BEFORE evaluate() -- which
        # may itself close an already-open trade internally (see
        # ai_trading_engine._check_open_trade_exit()) -- so the exit-side
        # block below can tell, by diffing against list_open_trades() again
        # after evaluate() returns, exactly which trade_id(s) closed THIS
        # cycle. Reads nothing else and changes no behavior; gated off by
        # default so this costs one extra read only when the shadow flag is
        # actually on.
        open_trades_before = ti_store.list_open_trades(symbol=symbol) if config.TI_ENABLE_EXECUTION_STATE_SHADOW else []
        rec = ai_trading_engine.evaluate(
            symbol, snapshot=snapshot, findings=ii.get("findings", []),
            capital=config.TI_DEFAULT_CAPITAL, risk_pct=config.TI_DEFAULT_RISK_PCT, expiry_date=symbol_expiry,
        )
        trade_id = (
            paper_trading.enter_from_recommendation(rec, snapshot=snapshot, findings=ii.get("findings", []))
            if rec.action in ("BUY CE", "BUY PE") else None
        )
        # Post-launch upgrade, Phase B1/B2: execution_state shadow
        # observation -- advisory/persisted-only, gated off by default
        # (config.TI_ENABLE_EXECUTION_STATE_SHADOW). Fires exactly when a
        # real (paper) position was actually opened this cycle -- the same
        # condition trade_id is not None already reports -- reusing
        # trade_id (ti_paper_trades' own primary key) as the execution_id
        # rather than inventing a second identity scheme. execution_state.py
        # makes no broker call and has no optional dependency (see its own
        # module docstring), so unlike signal_graph above this needs no
        # import-time guard -- only a runtime try/except, so a bug in this
        # wiring can never affect the real cycle above, which has already
        # fully completed (paper-trade entry) by this point.
        #
        # Phase B2 addition: paper mode has no distinct broker order
        # lifecycle -- a paper "fill" is instantaneous, so immediately after
        # APPROVED this chains the SAME execution straight through to
        # MONITORING (the hub state a real broker adapter would eventually
        # reach after READY/ORDER_INTENT/SUBMITTED/FILLED), where it sits
        # until the exit-side block below detects the underlying paper trade
        # has closed.
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
                    expiry_date=symbol_expiry.isoformat() if symbol_expiry else None,
                )
                execution_state.transition(
                    execution_id, "APPROVED",
                    reason="risk gate, position sizing, and market-session checks already passed -- paper trade opened",
                )
                _advance_execution_states(
                    execution_id, ("READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"),
                    reason="paper mode -- no distinct broker order lifecycle, advanced straight to MONITORING",
                )
            except Exception as e:
                log.warning(f"execution_state shadow wiring failed for {symbol!r}: {e}")
        # Post-launch upgrade, Phase B2: execution_state exit-side wiring
        # -- see _reconcile_execution_state_exits()'s own docstring for
        # why this must run here (not only in the caller that opened the
        # trade -- ai_trading_engine.evaluate() can close a trade from
        # ANY caller, including a plain dashboard read).
        if config.TI_ENABLE_EXECUTION_STATE_SHADOW and open_trades_before:
            _reconcile_execution_state_exits(symbol, open_trades_before)
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
        # MARKET_SNAPSHOT_INTEGRITY_AUDIT.md (2026-08-24): the cycle
        # timestamp this recommendation was actually computed from, so the
        # Telegram message can disclose its own age instead of looking
        # identical whether it's 3 minutes or 3 hours old.
        "as_of_ts": rec.as_of_ts,
        # Expiry-integrity scoped fix (2026-08-24): the exact option
        # contract this signal is on. evaluate() already fails closed
        # before returning an actionable BUY with expiry_date_resolved=
        # None (see ai_trading_engine.evaluate()'s own EXPIRY_NOT_RESOLVED
        # gate), so any signal reaching this payload always has a real
        # expiry_date; trading_symbol/token are None only when the
        # underlying strikes row predates the contract-identity
        # persistence migration.
        "expiry_date": rec.expiry_date_resolved.isoformat() if rec.expiry_date_resolved else None,
        "trading_symbol": rec.trading_symbol,
        "token": rec.token,
        # Signal Intelligence V2 (2026-08-24): shadow-only production
        # qualification verdict -- None whenever config.TI_ENABLE_SIGNAL_QUALITY_V2
        # is off. See ai_trading_engine.Recommendation's own docstring: never
        # changes what this payload's other fields say, purely an additional
        # label telegram_notifier.py can render when present.
        "production_action": rec.production_action,
        "production_explanation": rec.production_explanation,
    }


# Post-launch upgrade: get_overview() latency fix. Measured directly
# against production before this change: 11 watched symbols evaluated
# SEQUENTIALLY took ~63s total (each symbol's own get_symbol_overview()
# is a genuinely non-trivial 3-9s -- market snapshot + institutional
# sweep + strike table + AI recommendation + multi-timeframe summary,
# no single catastrophic outlier). The client's own dashboard polls
# this exact endpoint every 15s -- a 63s response means requests
# permanently pile up faster than they complete, which is why symbol
# tabs beyond the hardcoded NIFTY default frequently never populated.
#
# _OVERVIEW_EXECUTOR: one worker per watched symbol -- get_symbol_
# overview() calls for DIFFERENT symbols read/write disjoint data (no
# two symbols share a mutable object), so running them concurrently is
# safe; this is the exact same real-concurrency tool (stdlib
# ThreadPoolExecutor) agents/runtime/task_queue.py already uses, and
# this app's own SocketIO server already runs on async_mode="threading"
# (real OS threads, not eventlet green threads -- see app.py's own
# SocketIO(...) call) -- so this introduces no new concurrency model,
# only reuses the one already running this whole app.
_OVERVIEW_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(len(config.TI_WATCHED_SYMBOLS), 1), thread_name_prefix="ti-overview",
)

# Short-TTL cache with stampede protection -- config.
# TI_OVERVIEW_CACHE_TTL_SECONDS (default well under the client's own
# 15s auto-refresh interval), so an identical poll within the TTL
# window returns instantly instead of re-running all 11 symbols from
# scratch. _OVERVIEW_CACHE_LOCK ensures at most ONE recompute runs at
# a time -- a request that arrives while a recompute is already
# in-flight gets the last genuinely-computed result (stale by at most
# one TTL window, never fabricated) rather than starting a second,
# wasted, fully-concurrent 11-symbol sweep of its own.
_overview_cache = {"ts": 0.0, "data": None}
_OVERVIEW_CACHE_LOCK = threading.Lock()


def _compute_overview(*, expiry_date: dt.date | None, expiry_dates: dict | None) -> dict:
    """The real, uncached, parallelized build every get_overview() call
    eventually bottoms out in. A symbol whose read raises is isolated
    here -- it reports its own {"available": False, "reason": ...},
    exactly the same shape a genuinely-unavailable snapshot already
    produces, never taking down the other symbols' results."""
    def _one(symbol: str) -> tuple:
        try:
            return symbol, get_symbol_overview(symbol, expiry_date=(expiry_dates or {}).get(symbol, expiry_date))
        except Exception as e:
            log.warning(f"get_overview: {symbol!r} failed (isolated, other symbols unaffected): {e}")
            return symbol, {"symbol": symbol, "available": False, "reason": f"internal error: {e}"}

    # ThreadPoolExecutor.map() yields results in the SAME order as the
    # input iterable regardless of completion order, so this dict's key
    # order matches config.TI_WATCHED_SYMBOLS exactly, same as the
    # original sequential dict comprehension -- response structure is
    # unchanged.
    results = dict(_OVERVIEW_EXECUTOR.map(_one, config.TI_WATCHED_SYMBOLS))

    return {
        "symbols": results,
        "paper_trading": get_paper_trading_summary(),
        "agent_health": sysadmin_api.get_agent_status(),
    }


def get_overview(*, expiry_date: dt.date | None = None, expiry_dates: dict | None = None,
                  use_cache: bool = True) -> dict:
    """The full Trading Intelligence Dashboard: every watched symbol's
    overview, paper trading performance, and Agent Health (reused from
    agents.sys_admin.api, not re-queried).

    `expiry_dates`: see run_scheduled_cycle()'s own docstring for why this
    is per-symbol, not one shared date, and why `expiry_date` alone
    remains a valid fallback. Not part of the cache key -- expiry dates
    genuinely change at most once a day/week, never within a single
    TI_OVERVIEW_CACHE_TTL_SECONDS window, so this is a safe, documented
    simplification, not an oversight.

    `use_cache=False` always recomputes fresh -- every existing test/
    caller that needs a guaranteed-current read (not the live dashboard
    poll this cache exists for) keeps working exactly as before."""
    if not use_cache:
        return _compute_overview(expiry_date=expiry_date, expiry_dates=expiry_dates)

    now = time.monotonic()
    cached = _overview_cache["data"]
    if cached is not None and (now - _overview_cache["ts"]) < config.TI_OVERVIEW_CACHE_TTL_SECONDS:
        return cached

    acquired = _OVERVIEW_CACHE_LOCK.acquire(blocking=False)
    if not acquired:
        if cached is not None:
            return cached
        _OVERVIEW_CACHE_LOCK.acquire()  # first-ever call, a recompute is already in flight -- nothing to serve yet
    try:
        # Double-checked locking: a thread that just spent time waiting
        # for the lock above (the "first-ever call" branch) may find the
        # cache already fresh -- the thread that held the lock just
        # finished and populated it. Re-check before recomputing, or
        # every waiting thread would each run its own full 11-symbol
        # sweep in turn as the lock passes from one to the next (a real
        # bug caught by test_overlapping_requests_do_not_each_trigger_
        # their_own_recompute -- the original version had 8 concurrent
        # callers produce 8 full recomputes instead of 1).
        cached = _overview_cache["data"]
        if cached is not None and (time.monotonic() - _overview_cache["ts"]) < config.TI_OVERVIEW_CACHE_TTL_SECONDS:
            return cached
        data = _compute_overview(expiry_date=expiry_date, expiry_dates=expiry_dates)
        _overview_cache["data"] = data
        _overview_cache["ts"] = time.monotonic()
        return data
    finally:
        _OVERVIEW_CACHE_LOCK.release()
