"""
agents/trading_intelligence/structure_overlay.py -- Milestone 20, Phase 5:
read-only "what is this symbol's structure doing right now" query,
backing the dashboard's Structure Overlay panel
(GET /api/structure/<symbol>/overlay).

Deliberately separate from structure_alerts.py: that module's job is
deciding whether to SEND something (Telegram + in-memory dedup state,
real side effects); this module's only job is to ANSWER a read -- it
never touches _last_alert_by_key, never calls telegram_notifier, never
renders a chart. It reuses the exact same institutional_levels.py
functions (weighted_levels/detect_role_reversal/classify_market_state/
compute_trade_plan_overlay/best_candidate_level) and structure_alerts.py's
own _nearest_reversal_levels()/_timeframe_confirmation_label() helpers,
so the numbers shown here always match what a real alert would have
computed for the same input -- no parallel confidence formula, no
second source of truth.

Not gated by config.TI_ENABLE_STRUCTURE_ALERTS: showing the current
structure state is a plain read of already-live data and carries none
of that flag's risk (a Telegram send) -- the flag stays scoped to what
it was actually built to gate.
"""
import datetime as dt

import institutional_levels as il

from ..runtime import market_session
from . import data_access, market_data, structure_alerts

# Returned as `state` when weighted_levels() has nothing at/above
# MAJOR_LEVEL_MIN_WEIGHT this cycle -- distinct from any real
# classify_market_state() state name so a UI can style/label it
# honestly as "no major level yet" instead of implying a real state.
NO_MAJOR_LEVEL = "NO_MAJOR_LEVEL"


def _pick_level(symbol: str, *, candles: list, snapshot) -> dict | None:
    """Picks the one level/state to show for this symbol: prefer an
    alertable state (matching structure_alerts.py's own
    _ALERTABLE_STATES) with the highest confidence; among non-alertable
    states, still show the highest-confidence one rather than nothing.
    Falls back to best_candidate_level() (same function the preview-
    chart feature already uses) when weighted_levels() returns no Major
    Institutional Level at all this cycle."""
    levels = il.weighted_levels(
        symbol, candles=candles, rows=snapshot.strikes, atm=snapshot.atm, underlying=snapshot.underlying_ltp,
    )
    profile = il.get_profile(symbol)

    best = None
    for lvl in levels:
        reversal = il.detect_role_reversal(lvl["level"], candles, profile=profile)
        state_result = il.classify_market_state(
            symbol, candles=candles, levels=[lvl], underlying=snapshot.underlying_ltp, vwap=snapshot.vwap,
        )
        state = state_result["state"]
        confidence = reversal["confidence"] if reversal else round(abs(state_result["score"]) * 100)
        candidate = {"levels": levels, "level": lvl, "reversal": reversal, "state": state,
                     "confidence": confidence, "is_major": True}

        is_alertable = state in structure_alerts._ALERTABLE_STATES
        best_is_alertable = best is not None and best["state"] in structure_alerts._ALERTABLE_STATES
        if best is None or (is_alertable and not best_is_alertable) or \
                (is_alertable == best_is_alertable and confidence > best["confidence"]):
            best = candidate

    if best is not None:
        return best

    candidate = il.best_candidate_level(symbol, candles=candles, rows=snapshot.strikes,
                                         atm=snapshot.atm, underlying=snapshot.underlying_ltp)
    if candidate is None:
        return None
    return {"levels": [], "level": {"level": candidate["level"], "type": candidate["type"]},
            "reversal": None, "state": NO_MAJOR_LEVEL,
            "confidence": round(candidate["weight"] * 100), "is_major": False}


def compute_overlay(symbol: str, *, snapshot=None, candles: list | None = None, now: dt.datetime | None = None) -> dict:
    """One symbol's current structure overlay -- always returns a dict
    describing what happened either way (available or not, with an
    honest reason), same discipline as structure_alerts.evaluate_symbol().
    `snapshot`/`candles`: pass these when the caller already fetched
    them this cycle; left None, this fetches them itself."""
    now = now or dt.datetime.now()

    exchange = market_session.EXCHANGE_MAP.get(symbol, "NSE")
    is_open, closed_reason = market_session.is_exchange_open(exchange)
    if not is_open:
        return {"symbol": symbol, "available": False, "reason": f"market closed ({closed_reason})"}

    if snapshot is None:
        snapshot = market_data.get_snapshot(symbol)
    if not snapshot.available:
        return {"symbol": symbol, "available": False, "reason": f"no OI snapshot ({snapshot.reason})"}

    if candles is None:
        df = data_access.load_fresh_candles(symbol)
        candles = df.to_dict("records") if not df.empty else []
    if not candles:
        return {"symbol": symbol, "available": False, "reason": "no candle data"}

    picked = _pick_level(symbol, candles=candles, snapshot=snapshot)
    if picked is None:
        return {"symbol": symbol, "available": True, "state": None, "reason": "no candidate level this cycle"}

    level_value = picked["level"]["level"]
    reversal = picked["reversal"]
    state = picked["state"]

    reversal_support = reversal_resistance = None
    if picked["levels"]:
        reversal_support, reversal_resistance = structure_alerts._nearest_reversal_levels(picked["levels"], level_value)

    # Support/resistance shown to the reader: for a confirmed role
    # reversal, the flipped level itself is one side and the nearest
    # other Major Institutional Level is the other (same convention
    # structure_chart.py's own support_level/resistance_level already
    # use). Outside a reversal (RANGE/TRENDING/NO_MAJOR_LEVEL), there's
    # no bullish/bearish role to hang a side on -- fall back to the raw
    # cluster's own type instead of guessing one.
    if reversal is not None:
        is_bullish = reversal["current_role"] == "SUPPORT"
        support = level_value if is_bullish else reversal_support
        resistance = reversal_resistance if is_bullish else level_value
    else:
        support = level_value if picked["level"]["type"] == "SUPPORT" else reversal_support
        resistance = level_value if picked["level"]["type"] == "RESISTANCE" else reversal_resistance

    result = {
        "symbol": symbol, "available": True, "state": state, "is_major_level": picked["is_major"],
        "confidence": picked["confidence"], "level": level_value,
        "support": support, "resistance": resistance,
        "reversal_support": reversal_support, "reversal_resistance": reversal_resistance,
        "as_of": now.isoformat(),
    }

    if reversal:
        result["previous_role"] = reversal["previous_role"]
        result["current_role"] = reversal["current_role"]
        overlay = il.compute_trade_plan_overlay(symbol, reversal)
        if overlay:
            option_strike = il.pick_option_strike(snapshot.strikes, snapshot.atm, overlay["direction"])
            if option_strike:
                overlay = {**overlay, "option_strike": option_strike}
            result["overlay"] = overlay
            tf_label = structure_alerts._timeframe_confirmation_label(symbol, overlay["direction"])
            if tf_label:
                result["timeframe"] = tf_label

    return result
