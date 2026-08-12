"""
agents/trading_intelligence/telegram_notifier.py -- Milestone 19: Telegram
signal notifications sourced ONLY from this package's own AI orchestration
(ai_trading_engine.evaluate(), reached via api.run_scheduled_cycle()) --
never from sr_engine/paper-trade execution/LTP tracking. Replaces the
Milestone 18 wiring in app.py's update_paper_trading(), which posted to
the same channel from the S/R Engine's continuous scalping-trade
lifecycle (open/progress/close) -- that call sites are now disabled (see
app.py's own comments at each removed call site), not deleted, per the
explicit instruction that stopped them.

This module fires ONCE per newly-generated actionable recommendation --
nothing else. No trade fills, no trailing-stop/LTP updates, no partial
exits, no target-hit notices, no paper P&L, no backtest events. The
caller (agents/trading_intelligence/api.py's run_scheduled_cycle())
decides WHETHER to call send_trading_intelligence_signal() (actionable +
confidence gate); this module only formats and delivers.

Field honesty note: this codebase's Recommendation dataclass
(ai_trading_engine.py) does not compute a numeric "institutional score"
or categorical premium-momentum/OI-structure/VWAP-structure/repeated-
rejection labels -- those concepts exist informally as free-text
reasoning (institutional_reasoning/oi_reasoning/greeks_reasoning/
price_action_reasoning). Rather than fabricate numbers this engine never
actually computed, the real call site passes those reasoning strings
under `reasoning_details`; the formatter below renders whichever of
BOTH shapes (structured AI-factor fields OR free-text reasoning_details)
the caller actually supplied, never inventing a value for a key that's
missing.
"""
import datetime as dt
import logging
import os

import requests

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SIGNALS_CHANNEL_ID = os.getenv("TELEGRAM_SIGNALS_CHANNEL_ID", "")

DEDUP_WINDOW_SECONDS = 300  # 5 minutes, per spec

# In-memory only -- a process restart naturally clears it, which is fine:
# a genuinely repeated signal right after a restart is still worth one
# fresh alert, not silently suppressed by pre-restart history.
_last_sent_by_fingerprint: dict[tuple, dt.datetime] = {}


def _signal_fingerprint(payload: dict) -> tuple:
    """(symbol, signal_type, strike, entry_price rounded to 1 decimal) --
    exactly the spec's fingerprint. Reads strike/entry_price from either
    a top-level "strike"/"entry_price" pair or a nested
    "entry_zone": {"strike":..., "price":...} dict (the example payload's
    own shape) -- whichever the caller used."""
    entry_zone = payload.get("entry_zone")
    if isinstance(entry_zone, dict):
        strike = entry_zone.get("strike")
        entry_price = entry_zone.get("price")
    else:
        strike = payload.get("strike")
        entry_price = payload.get("entry_price")
    return (
        payload.get("symbol"),
        payload.get("signal_type"),
        strike,
        round(entry_price, 1) if entry_price is not None else None,
    )


def _is_duplicate(fingerprint: tuple, *, now: dt.datetime) -> bool:
    last = _last_sent_by_fingerprint.get(fingerprint)
    return last is not None and (now - last).total_seconds() < DEDUP_WINDOW_SECONDS


def _fmt_price(value) -> str:
    return "?" if value is None else (f"{value:g}" if isinstance(value, (int, float)) else str(value))


def _format_html(payload: dict) -> str:
    symbol = payload.get("symbol", "?")
    signal_type = payload.get("signal_type", "?")
    direction = "CE" if signal_type.endswith("CE") else ("PE" if signal_type.endswith("PE") else "?")
    bias = payload.get("overall_bias") or payload.get("market_bias") or "?"
    confidence = payload.get("confidence")

    entry_zone = payload.get("entry_zone")
    if isinstance(entry_zone, dict):
        strike, entry_price = entry_zone.get("strike"), entry_zone.get("price")
    else:
        strike, entry_price = payload.get("strike"), payload.get("entry_price")

    targets = payload.get("targets") or []
    stop_loss = payload.get("stop_loss") if payload.get("stop_loss") is not None else payload.get("sl_price")

    lines = [
        "\U0001F6A8 <b>IDaddy AI Trading Intelligence</b>", "",
        f"\U0001F4CA <b>{symbol}</b>",
        f"\U0001F9ED Bias: <b>{bias}</b>",
    ]
    if confidence is not None:
        lines.append(f"\U0001F4C8 Confidence: <b>{confidence}%</b>")
    lines += [
        "",
        "\U0001F3AF <b>Suggested Trade</b>",
        f"{'BUY' if direction == 'CE' else 'SELL'} {_fmt_price(strike)} {direction} ABOVE <b>{_fmt_price(entry_price)}</b>",
    ]
    if targets:
        lines += ["", "\U0001F4B0 <b>Targets</b>"]
        for i, t in enumerate(targets[:3], start=1):
            lines.append(f"T{i}: {_fmt_price(t)}")
    if stop_loss is not None:
        lines += ["", "\U0001F6D1 <b>Stop Loss</b>", _fmt_price(stop_loss)]

    # AI Factors: only the keys the caller actually supplied -- never a
    # fabricated line for a field this engine didn't genuinely compute.
    factor_lines = []
    if payload.get("institutional_score") is not None:
        factor_lines.append(f"• Institutional Score: {payload['institutional_score']}")
    if payload.get("premium_momentum") is not None:
        factor_lines.append(f"• Premium Momentum: {payload['premium_momentum']}")
    if payload.get("oi_structure") is not None:
        factor_lines.append(f"• OI Structure: {payload['oi_structure']}")
    if payload.get("vwap_structure") is not None:
        factor_lines.append(f"• VWAP: {payload['vwap_structure']}")
    if payload.get("repeated_rejection") is not None:
        factor_lines.append(f"• Repeated Rejection: {'YES' if payload['repeated_rejection'] else 'NO'}")
    if payload.get("price_action_bias"):
        factor_lines.append(f"• Price Action: {payload['price_action_bias']}")
    for detail in (payload.get("reasoning_details") or []):
        if detail:
            factor_lines.append(f"• {detail}")
    if factor_lines:
        lines += ["", "\U0001F9E0 <b>AI Factors</b>", *factor_lines]

    if payload.get("reasoning"):
        lines += ["", f"\U0001F4DD {payload['reasoning']}"]

    lines += [
        "",
        f"⏰ {dt.datetime.now().strftime('%I:%M %p')}",
        "⚠️ Educational purpose only",
    ]
    return "\n".join(lines)


def send_trading_intelligence_signal(payload: dict) -> bool:
    """Formats and sends one Trading-Intelligence signal message to
    TELEGRAM_SIGNALS_CHANNEL_ID. Returns False (without raising) if the
    channel isn't configured, if this exact signal was already sent
    within DEDUP_WINDOW_SECONDS, or if the send itself fails -- matching
    every other Telegram sender in this codebase's fire-and-forget,
    never-raise contract."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_SIGNALS_CHANNEL_ID):
        log.warning("Trading Intelligence Telegram signal skipped -- "
                     "TELEGRAM_BOT_TOKEN/TELEGRAM_SIGNALS_CHANNEL_ID not configured.")
        return False

    now = dt.datetime.now()
    fingerprint = _signal_fingerprint(payload)
    if _is_duplicate(fingerprint, now=now):
        log.info(f"Trading Intelligence Telegram signal suppressed (duplicate within "
                 f"{DEDUP_WINDOW_SECONDS}s): {fingerprint}")
        return False

    msg = _format_html(payload)
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_SIGNALS_CHANNEL_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Trading Intelligence Telegram send failed: {e}")
        return False

    _last_sent_by_fingerprint[fingerprint] = now
    log.info(
        "Trading Intelligence Telegram signal sent",
        extra={"symbol": payload.get("symbol"), "signal_type": payload.get("signal_type"),
               "confidence": payload.get("confidence")},
    )
    return True
