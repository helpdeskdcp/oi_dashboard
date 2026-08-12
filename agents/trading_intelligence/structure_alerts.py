"""
agents/trading_intelligence/structure_alerts.py -- Milestone 20, Phase 2:
wires institutional_levels.py's weighted_levels()/detect_role_reversal()/
classify_market_state() into the live Trading Intelligence cycle, in
STRUCTURE-ALERT-ONLY mode.

Explicitly NOT this module's job (per the request that authorized it):
open a paper trade, send a BUY CE/PE signal, touch position sizing, or
change anything about ai_trading_engine.evaluate()'s own decision. This
module only ever calls telegram_notifier.send_structure_update() --
never paper_trading.enter_from_recommendation(), never
telegram_notifier.send_trading_intelligence_signal(). A structure alert
describes what the MARKET did (a level's role flipped, or a live
direction state), never what to DO about it -- ai_trading_engine.py/
oi_engine.generate_signal() remain the only place a trade decision is
made, completely unchanged by this module's existence.

Gated by config.TI_ENABLE_STRUCTURE_ALERTS (default False) -- deploying
this file changes nothing about the live cycle until that flag is
explicitly turned on. Runs a symbol's evaluation only once its own
exchange session is open (market_session.is_exchange_open()), an OI
snapshot is available (market_data.get_snapshot()), and candle data
exists (data_access.load_candles()) -- an honest "not evaluated" with a
reason otherwise, never a guess from partial data.
"""
import datetime as dt
import logging

import institutional_levels as il

from .. import config
from ..runtime import market_session
from . import data_access, market_data, multi_timeframe, structure_chart, telegram_notifier

log = logging.getLogger("oi_dashboard.trading_intelligence.structure_alerts")

# Which classify_market_state() states are worth alerting on at all --
# matches the 7 alert types explicitly allowed for this deployment
# (role flips surface via BULLISH/BEARISH_RETEST_ACTIVE; TRENDING_UP/
# DOWN and RANGE are informational states, not alert-worthy on their
# own for this phase).
_ALERTABLE_STATES = frozenset({
    il.BULLISH_RETEST_ACTIVE, il.BEARISH_RETEST_ACTIVE,
    il.BREAKOUT_WATCH, il.BREAKDOWN_WATCH, il.REVERSAL_RISK,
})

DEDUP_WINDOW_SECONDS = 900  # 15 minutes, per spec

# In-memory, keyed (symbol, state, level) -- NOT candle_time: a literal
# candle_time in the key would change every scheduler tick (a new
# candle every 3 minutes) and defeat a 15-minute suppression window
# entirely. This achieves the same real intent ("don't repeat this
# exact alert for 15 minutes") via time-based expiry against the
# (symbol, state, level) identity instead.
_last_alert_by_key: dict[tuple, dt.datetime] = {}


def _is_recent_duplicate(key: tuple, *, now: dt.datetime) -> bool:
    last = _last_alert_by_key.get(key)
    return last is not None and (now - last).total_seconds() < DEDUP_WINDOW_SECONDS


def _nearest_reversal_levels(levels: list, current_level: float) -> tuple:
    """(reversal_support, reversal_resistance) -- the nearest OTHER
    Major Institutional Level below/above `current_level` from the same
    already-computed weighted_levels() list. Real, already-scored
    levels (never a new computation) -- "if this level fails, here's
    the next real one" context, not a fabricated formula (the spec gave
    no explicit one for these two fields)."""
    supports_below = [lv["level"] for lv in levels if lv["type"] == "SUPPORT" and lv["level"] < current_level]
    resistances_above = [lv["level"] for lv in levels if lv["type"] == "RESISTANCE" and lv["level"] > current_level]
    reversal_support = max(supports_below) if supports_below else None
    reversal_resistance = min(resistances_above) if resistances_above else None
    return reversal_support, reversal_resistance


def _timeframe_confirmation_label(symbol: str, direction: str) -> str | None:
    """Real (not fabricated) multi-timeframe agreement check, reusing
    multi_timeframe.get_timeframe() -- a timeframe counts as
    "confirmed" only when ITS OWN recent candles actually moved the
    same direction as the reversal. Returns None (the TF line is simply
    omitted) when neither 3m nor 5m can be confirmed, rather than
    printing a confirmation that isn't real."""
    confirmed = []
    for tf in ("3m", "5m"):
        result = multi_timeframe.get_timeframe(symbol, tf)
        if not result["available"] or len(result["candles"]) < 2:
            continue
        recent = result["candles"].tail(5)
        moved_up = recent.iloc[-1]["close"] > recent.iloc[0]["close"]
        if (direction == "BULLISH" and moved_up) or (direction == "BEARISH" and not moved_up):
            confirmed.append(tf)
    return " + ".join(confirmed) + " confirmed" if confirmed else None


def evaluate_symbol(symbol: str, *, snapshot=None, candles: list | None = None, now: dt.datetime | None = None) -> dict:
    """One symbol's structure evaluation for this cycle. Returns a dict
    describing what happened either way (evaluated or not, and which
    alerts were actually sent) -- useful both for the live cycle's own
    findings/logging and for a forced manual verification call.

    `snapshot`/`candles`: pass these when the caller already fetched
    them this cycle (the same "avoid a second fetch for the same data"
    discipline every other trading_intelligence entrypoint already
    holds to); left None, this fetches them itself."""
    now = now or dt.datetime.now()

    if not config.TI_ENABLE_STRUCTURE_ALERTS:
        return {"symbol": symbol, "evaluated": False, "reason": "TI_ENABLE_STRUCTURE_ALERTS is False"}

    exchange = market_session.EXCHANGE_MAP.get(symbol, "NSE")
    is_open, closed_reason = market_session.is_exchange_open(exchange)
    if not is_open:
        return {"symbol": symbol, "evaluated": False, "reason": f"market closed ({closed_reason})"}

    if snapshot is None:
        snapshot = market_data.get_snapshot(symbol)
    if not snapshot.available:
        return {"symbol": symbol, "evaluated": False, "reason": f"no OI snapshot ({snapshot.reason})"}

    if candles is None:
        df = data_access.load_candles(symbol)
        candles = df.to_dict("records") if not df.empty else []
    if not candles:
        return {"symbol": symbol, "evaluated": False, "reason": "no candle data"}

    levels = il.weighted_levels(
        symbol, candles=candles, rows=snapshot.strikes, atm=snapshot.atm, underlying=snapshot.underlying_ltp,
    )
    profile = il.get_profile(symbol)

    alerts_sent = []
    for lvl in levels:
        reversal = il.detect_role_reversal(lvl["level"], candles, profile=profile)
        state_result = il.classify_market_state(
            symbol, candles=candles, levels=[lvl], underlying=snapshot.underlying_ltp, vwap=snapshot.vwap,
        )
        state = state_result["state"]
        confidence = reversal["confidence"] if reversal else round(abs(state_result["score"]) * 100)

        if state not in _ALERTABLE_STATES:
            log.info(f"[STRUCTURE] SYMBOL={symbol} STATE={state} LEVEL={lvl['level']} CONF={confidence} ALERT_SENT=false")
            continue

        key = (symbol, state, lvl["level"])
        if _is_recent_duplicate(key, now=now):
            log.info(f"[STRUCTURE] SYMBOL={symbol} STATE={state} LEVEL={lvl['level']} CONF={confidence} "
                     f"ALERT_SENT=false (duplicate within {DEDUP_WINDOW_SECONDS}s)")
            continue

        payload = {"symbol": symbol, "level": lvl["level"], "state": state, "confidence": confidence}
        reversal_support = reversal_resistance = None
        if reversal:
            payload["previous_role"] = reversal["previous_role"]
            payload["current_role"] = reversal["current_role"]
            reversal_support, reversal_resistance = _nearest_reversal_levels(levels, lvl["level"])

            overlay = il.compute_trade_plan_overlay(symbol, reversal)
            if overlay:
                payload["overlay"] = overlay
                if reversal_support is not None:
                    payload["reversal_support"] = reversal_support
                if reversal_resistance is not None:
                    payload["reversal_resistance"] = reversal_resistance
                tf_label = _timeframe_confirmation_label(symbol, overlay["direction"])
                if tf_label:
                    payload["timeframe"] = tf_label

        # One JPEG per alert actually sent -- never more than once per
        # (symbol, state, level) per DEDUP_WINDOW_SECONDS, since a
        # duplicate already `continue`s above before reaching here.
        # Best-effort: render_structure_chart() never raises, returns
        # None on any failure, and send_structure_update() below already
        # sends the text alert regardless of whether a chart exists.
        chart_path = structure_chart.render_structure_chart(
            symbol, candles, level=lvl["level"], state=state, reversal=reversal,
            overlay=payload.get("overlay"), confidence=confidence,
            reversal_support=reversal_support, reversal_resistance=reversal_resistance,
        )

        sent = telegram_notifier.send_structure_update(payload, chart_path=chart_path)
        log.info(f"[STRUCTURE] SYMBOL={symbol} STATE={state} LEVEL={lvl['level']} CONF={confidence} "
                 f"ALERT_SENT={sent} CHART={'yes' if chart_path else 'no'}")
        if sent:
            _last_alert_by_key[key] = now
            alerts_sent.append(payload)

    return {"symbol": symbol, "evaluated": True, "levels_checked": len(levels), "alerts_sent": alerts_sent}


def run_structure_alert_cycle(symbols=None) -> dict:
    """Evaluates every currently-active symbol (or `symbols` if given,
    for a forced/manual check) for a structure alert. This is what
    agents.runtime.agent_runtime._trading_intelligence_cycle() calls --
    a step entirely SEPARATE from ai_trading_engine.evaluate()/
    paper_trading.enter_from_recommendation(), never touching either.
    Returns {} immediately (no per-symbol evaluation, no candle/OI
    fetches) when config.TI_ENABLE_STRUCTURE_ALERTS is False."""
    if not config.TI_ENABLE_STRUCTURE_ALERTS:
        return {}
    candidates = symbols if symbols is not None else market_session.active_symbols(config.TI_WATCHED_SYMBOLS)
    return {sym: evaluate_symbol(sym) for sym in candidates}
