"""
agents/trading_intelligence/ai_live_snapshot.py -- Milestone 21, Phase 3:
the AI Live Analysis Snapshot. A single, compact, per-symbol read
combining fields already computed/stored across this package and the
root-level engines it wraps -- spot/ATM/CE-PE chain data
(data_access.latest_cycle()), ADX/ATR/VWAP/prior-day levels
(data_access.latest_market_structure()), classical pivots
(market_structure.classical_pivots(), applied to the stored pdh/pdl/pdc
-- never recomputed here), OI walls (oi_engine.oi_walls()), multi-
timeframe RSI (agents.trading_intelligence.multi_timeframe.get_timeframe()'s
already-fresh-merged candles), EMA9/21 on the 3m timeframe (this
codebase's own default scalping timeframe -- candle_recorder.py,
institutional_levels.py's overlay, and structure_alerts.py all default
to "3m"), and institutional bias/AI confidence
(intelligence_orchestrator.build_snapshot()).

Every read here is broker-free: no function in this module ever fetches
from Angel One directly, and the dashboard's 1-second auto-refresh timer
just re-runs this same cheap, already-stored-data read -- it is NOT a
new polling loop against the broker, see this package's own __init__.py
safety rule (verified by test_safety.py's AST scan).

A field is None (never fabricated) whenever its source data isn't
available yet this cycle -- same "honest gap over a made-up number"
discipline every other advisory module in this package follows.
"""
import datetime as dt

import market_structure as market_structure_module
import oi_engine
import intelligence_orchestrator

from . import data_access, multi_timeframe

RSI_WINDOW = 14
RSI_TIMEFRAMES = ("1m", "3m", "5m")
EMA_TIMEFRAME = "3m"   # this codebase's own default scalping timeframe (see module docstring)
EMA_PERIODS = (9, 21)


def _rsi(closes: list, *, window: int = RSI_WINDOW) -> float | None:
    """Standard Wilder-style RSI over a plain list of closes (oldest
    first) -- same formula as agents.quant_researcher.features._rsi(),
    reimplemented here in plain Python (that one is pandas-Series-typed
    and module-private, over a different candle loader) rather than
    imported across package boundaries for a different data shape."""
    if len(closes) < window + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-window:]) / window
    avg_loss = sum(losses[-window:]) / window
    if avg_gain == 0 and avg_loss == 0:
        return 50.0   # no movement at all over the window -- neutral, not "maximally overbought"
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema_series(values: list, period: int) -> list:
    """Same formula as dynamic_sr_engine.detect_trend()'s own nested
    ema_series -- duplicated here (that one is nested/module-private,
    not importable) rather than refactored, matching this package's own
    "never modify a prior milestone's file for a new concern" discipline."""
    if not values:
        return []
    k = 2 / (period + 1)
    series = [values[0]]
    for v in values[1:]:
        series.append(v * k + series[-1] * (1 - k))
    return series


def _latest_ema(closes: list, period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(_ema_series(closes, period)[-1], 2)


def _rsi_for_timeframe(symbol: str, timeframe: str) -> float | None:
    tf = multi_timeframe.get_timeframe(symbol, timeframe)
    candles = tf.get("candles")
    if candles is None or candles.empty:
        return None
    return _rsi(candles["close"].tolist())


def _intel_card(symbol: str) -> dict | None:
    try:
        snapshot = intelligence_orchestrator.build_snapshot(symbol)
    except Exception:
        return None
    return snapshot.to_dict() if snapshot else None


def build_ai_live_snapshot(symbol: str) -> dict | None:
    """The full per-symbol snapshot, or None if there's no cycle data at
    all yet for this symbol (an honest "nothing to show", not a
    partially-fabricated row)."""
    cycle_data = data_access.latest_cycle(symbol)
    if cycle_data is None:
        return None
    cycle, rows = cycle_data["cycle"], cycle_data["rows"]
    atm = cycle.get("atm")
    atm_row = next((r for r in rows if r.strike == atm), None)

    market_structure = data_access.latest_market_structure(symbol)
    pivots = None
    if market_structure and market_structure.get("pdh") and market_structure.get("pdl") and market_structure.get("pdc"):
        pivots = market_structure_module.classical_pivots(
            market_structure["pdh"], market_structure["pdl"], market_structure["pdc"],
        )

    support, resistance = oi_engine.oi_walls(rows) if rows else ([], [])

    intel = _intel_card(symbol)

    ema9 = ema21 = None
    tf3 = multi_timeframe.get_timeframe(symbol, EMA_TIMEFRAME)
    candles3 = tf3.get("candles")
    if candles3 is not None and not candles3.empty:
        closes3 = candles3["close"].tolist()
        ema9 = _latest_ema(closes3, EMA_PERIODS[0])
        ema21 = _latest_ema(closes3, EMA_PERIODS[1])

    rsi_by_tf = {tf: _rsi_for_timeframe(symbol, tf) for tf in RSI_TIMEFRAMES}

    underlying_ltp = cycle.get("underlying_ltp")
    vwap = market_structure.get("vwap") if market_structure else None
    vwap_distance = round(underlying_ltp - vwap, 2) if underlying_ltp is not None and vwap else None

    return {
        "timestamp": dt.datetime.now().isoformat(),
        "symbol": symbol,
        "spot_ltp": underlying_ltp,
        "atm_strike": atm,
        "ce_ltp": atm_row.ce_ltp if atm_row else None,
        "pe_ltp": atm_row.pe_ltp if atm_row else None,
        "ce_oi": atm_row.ce_oi if atm_row else None,
        "pe_oi": atm_row.pe_oi if atm_row else None,
        "ce_oi_chg": atm_row.ce_oi_chg if atm_row else None,
        "pe_oi_chg": atm_row.pe_oi_chg if atm_row else None,
        "pcr": cycle.get("pcr"),
        "ce_delta": atm_row.ce_delta if atm_row else None,
        "pe_delta": atm_row.pe_delta if atm_row else None,
        "ce_theta": atm_row.ce_theta if atm_row else None,
        "pe_theta": atm_row.pe_theta if atm_row else None,
        "ce_iv": atm_row.ce_iv if atm_row else None,
        "pe_iv": atm_row.pe_iv if atm_row else None,
        "vwap_distance": vwap_distance,
        "ema_9": ema9,
        "ema_21": ema21,
        "rsi_1m": rsi_by_tf.get("1m"),
        "rsi_3m": rsi_by_tf.get("3m"),
        "rsi_5m": rsi_by_tf.get("5m"),
        "adx": market_structure.get("adx") if market_structure else None,
        "atr": market_structure.get("atr_14") if market_structure else None,
        "pivot": pivots["P"] if pivots else None,
        "r1": pivots["R1"] if pivots else None,
        "s1": pivots["S1"] if pivots else None,
        "oi_wall_support": support[0].strike if support else None,
        "oi_wall_resistance": resistance[0].strike if resistance else None,
        "institutional_bias": intel["bias"] if intel else None,
        "ai_confidence": intel["confidence"] if intel else None,
    }


def to_telegram_text(snapshot: dict | None) -> str:
    """Compact, copy/paste-ready format for Telegram or pasting into
    ChatGPT -- plain text, no markup that either surface would mangle."""
    if not snapshot:
        return "No live snapshot available."
    return "\n".join([
        f"AI LIVE SNAPSHOT -- {snapshot['symbol']} @ {snapshot['timestamp']}",
        f"Spot: {snapshot['spot_ltp']} | ATM: {snapshot['atm_strike']}",
        f"CE LTP/PE LTP: {snapshot['ce_ltp']}/{snapshot['pe_ltp']} | PCR: {snapshot['pcr']}",
        f"CE OI/PE OI: {snapshot['ce_oi']}/{snapshot['pe_oi']} (chg {snapshot['ce_oi_chg']}/{snapshot['pe_oi_chg']})",
        f"CE Delta/PE Delta: {snapshot['ce_delta']}/{snapshot['pe_delta']} | "
        f"CE Theta/PE Theta: {snapshot['ce_theta']}/{snapshot['pe_theta']} | "
        f"CE IV/PE IV: {snapshot['ce_iv']}/{snapshot['pe_iv']}",
        f"VWAP dist: {snapshot['vwap_distance']} | EMA9/21: {snapshot['ema_9']}/{snapshot['ema_21']}",
        f"RSI 1m/3m/5m: {snapshot['rsi_1m']}/{snapshot['rsi_3m']}/{snapshot['rsi_5m']} | "
        f"ADX: {snapshot['adx']} | ATR: {snapshot['atr']}",
        f"Pivot/R1/S1: {snapshot['pivot']}/{snapshot['r1']}/{snapshot['s1']}",
        f"OI Wall S/R: {snapshot['oi_wall_support']}/{snapshot['oi_wall_resistance']}",
        f"Bias: {snapshot['institutional_bias']} | AI Confidence: {snapshot['ai_confidence']}",
    ])
