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

# Separate dict (Milestone 20) for send_structure_update()'s own
# fingerprint shape (symbol, level, current_role) -- kept apart from
# _last_sent_by_fingerprint above rather than sharing one dict with a
# type-tagged key, so existing signal-dedup tests/behavior are
# untouched by this addition.
_last_structure_update_by_fingerprint: dict[tuple, dt.datetime] = {}


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


def _is_duplicate(fingerprint: tuple, *, now: dt.datetime, store: dict | None = None) -> bool:
    last = (store if store is not None else _last_sent_by_fingerprint).get(fingerprint)
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
    # Two payload shapes both produce the same bullets: the real
    # Trading Intelligence wiring (api._build_telegram_payload()) sends
    # an already-filtered "reasoning_details" list; a caller that hasn't
    # bundled one yet (e.g. a demo/manual payload) may instead supply the
    # four reasoning strings directly as top-level keys -- fall back to
    # building the list from those only when reasoning_details itself
    # wasn't given or was empty, never merging both sources.
    reasoning_items = payload.get("reasoning_details")
    if not reasoning_items:
        reasoning_items = [
            payload.get("institutional_reasoning"),
            payload.get("oi_reasoning"),
            payload.get("greeks_reasoning"),
            payload.get("price_action_reasoning"),
        ]
    for detail in reasoning_items:
        if detail:
            factor_lines.append(f"• {detail}")
    if factor_lines:
        lines += ["", "\U0001F9E0 <b>AI Factors</b>", *factor_lines]

    if payload.get("reasoning"):
        lines += ["", f"\U0001F4DD {payload['reasoning']}"]

    footer = "⚠️ DEMO TEST — No trade executed" if payload.get("demo_test") is True else "⚠️ Educational purpose only"
    lines += [
        "",
        f"⏰ {dt.datetime.now().strftime('%I:%M %p')}",
        footer,
    ]
    return "\n".join(lines)


def _post_to_channel(msg: str) -> bool:
    """Shared HTTP POST to TELEGRAM_SIGNALS_CHANNEL_ID -- both
    send_trading_intelligence_signal() and send_structure_update() go
    through this one place rather than each doing their own requests.post()
    call. Never raises; returns False on any failure, matching this
    module's fire-and-forget contract."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_SIGNALS_CHANNEL_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def _post_photo_to_channel(filepath: str) -> bool:
    """Milestone 20, Phase 3: sendPhoto for a structure chart JPEG --
    ALWAYS a separate message from the text alert (_post_to_channel()
    above), never a single-message "photo with caption" -- Telegram
    caps photo captions at 1024 characters, well under what a full
    structure-update-with-overlay message can reach, so a single-message
    attachment would risk silently truncating real numbers (SL/targets/
    reversal levels). Two messages (text always, photo best-effort)
    guarantees the full text is always delivered intact. Never raises;
    False on any failure (missing file, network error, bad response)."""
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_SIGNALS_CHANNEL_ID},
                files={"photo": f},
                timeout=15,
            )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"Telegram photo send failed: {e}")
        return False


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

    if not _post_to_channel(_format_html(payload)):
        return False

    _last_sent_by_fingerprint[fingerprint] = now
    log.info(
        "Trading Intelligence Telegram signal sent",
        extra={"symbol": payload.get("symbol"), "signal_type": payload.get("signal_type"),
               "confidence": payload.get("confidence")},
    )
    return True


def _format_structure_update(payload: dict) -> str:
    """Milestone 20: a STRUCTURE-only alert -- describes what the market
    structure just did (a level's role flipped, OR a live direction
    state like BREAKOUT_WATCH/REVERSAL_RISK was classified), never a
    trade entry. Deliberately a different, less prominent header
    ("STRUCTURE UPDATE") than the "IDaddy AI Trading Intelligence"
    signal header above, so a reader can tell at a glance this isn't an
    actionable BUY/SELL.

    The role-flip line ("X SUPPORT -> RESISTANCE") only renders when
    the payload actually describes a flip (previous_role/current_role
    both given AND different) -- not every alertable state is a role
    flip (e.g. REVERSAL_RISK/BREAKOUT_WATCH are classify_market_state()
    reads on a level that hasn't necessarily flipped yet); for those,
    just the level and state are shown, never a fabricated "X -> X"
    non-flip line."""
    symbol = payload.get("symbol", "?")
    level = payload.get("level")
    previous_role = payload.get("previous_role")
    current_role = payload.get("current_role")
    confidence = payload.get("confidence")
    state = payload.get("state")

    lines = ["⚠️ <b>STRUCTURE UPDATE</b>", "", f"<b>{symbol}</b>", ""]
    if previous_role and current_role and previous_role != current_role:
        lines.append(f"{_fmt_price(level)} {previous_role} → {current_role}")
    elif level is not None:
        lines.append(f"Level: {_fmt_price(level)}")
    if confidence is not None:
        lines += ["", f"Composite confidence: {confidence}%"]

    major_support = payload.get("major_support")
    next_resistance = payload.get("next_resistance")
    if major_support is not None or next_resistance:
        lines.append("")
        if major_support is not None:
            lines.append(f"Major support: {_fmt_price(major_support)}")
        if next_resistance:
            joined = " / ".join(_fmt_price(v) for v in next_resistance)
            lines.append(f"Next resistance: {joined}")

    if state:
        lines += ["", f"State: {state}"]

    # Milestone 20, Phase 3: Trade Plan Overlay -- purely additive to
    # the existing format above (kept byte-for-byte unchanged when
    # `overlay` isn't in the payload, so nothing already relying on the
    # plain structure-update text breaks). institutional_levels.
    # compute_trade_plan_overlay() already enforces the >=75 confidence
    # gate and never returns a plan for a bad/degenerate risk -- this
    # function only ever RENDERS what it's given, never decides whether
    # to attach one.
    overlay = payload.get("overlay")
    if overlay:
        entry_label = "Buy Above" if overlay.get("direction") == "BULLISH" else "Buy PE Below"
        lines += [
            "", "📍 <b>Trade Plan Overlay</b>",
            f"{entry_label}: {_fmt_price(overlay['entry'])}",
            f"SL: {_fmt_price(overlay['sl'])}",
            f"T1: {_fmt_price(overlay['t1'])}",
            f"T2: {_fmt_price(overlay['t2'])}",
        ]

        # Purely additive (only renders when institutional_levels.
        # pick_option_strike() found a real ATM premium to quote) --
        # the actual tradeable option this structure read implies,
        # rather than only an underlying price level.
        option_strike = overlay.get("option_strike")
        if option_strike:
            lines.append(
                f"Option: {option_strike['strike']} {option_strike['option_type']} "
                f"@ {_fmt_price(option_strike['premium'])}"
            )

        reversal_support = payload.get("reversal_support")
        reversal_resistance = payload.get("reversal_resistance")
        if reversal_support is not None or reversal_resistance is not None:
            lines.append("")
            if reversal_support is not None:
                lines.append(f"🔄 Reversal Support: {_fmt_price(reversal_support)}")
            if reversal_resistance is not None:
                lines.append(f"🔄 Reversal Resistance: {_fmt_price(reversal_resistance)}")

        timeframe = payload.get("timeframe")
        if timeframe:
            lines += ["", f"⏰ TF: {timeframe}"]

        lines += ["", "⚠️ Informational structure overlay only — not an executed trade signal."]

    lines += ["", f"⏰ {dt.datetime.now().strftime('%I:%M %p')}"]
    return "\n".join(lines)


def _structure_fingerprint(payload: dict) -> tuple:
    """(symbol, level, current_role or state) -- a structure update
    re-alerts only when the role/state genuinely changes again, not
    every scheduler cycle the same reversal/classification happens to
    still be active (detect_role_reversal()/classify_market_state() are
    both stateless and would otherwise re-report the same result every
    3 minutes). Falls back to `state` when there's no role-flip
    (current_role) at all -- e.g. a BREAKOUT_WATCH/REVERSAL_RISK alert
    that isn't a flip still needs its own dedup key."""
    return (payload.get("symbol"), payload.get("level"), payload.get("current_role") or payload.get("state"))


def send_structure_update(payload: dict, *, chart_path: str | None = None) -> bool:
    """Formats and sends one structure (role-reversal) update to
    TELEGRAM_SIGNALS_CHANNEL_ID. Same no-op-when-unconfigured/dedup/
    never-raise contract as send_trading_intelligence_signal() above --
    this is NOT a trade signal (no entry/target/SL), so it's fingerprinted
    and dedup'd separately (by (symbol, level, current_role) rather than
    (symbol, signal_type, strike, entry_price)) so it re-alerts only when
    the role genuinely flips again, not on every scheduler tick the same
    reversal is still active.

    `chart_path` (Milestone 20, Phase 3): a JPEG rendered by
    structure_chart.render_structure_chart(), sent as a SEPARATE photo
    message after the text alert (see _post_photo_to_channel()'s own
    docstring for why never a single combined message). The function's
    own success/dedup are governed entirely by the TEXT send -- a
    missing/failed chart never blocks or fails the real alert, exactly
    matching "attach the image if generation succeeds; otherwise send
    the text alert normally.\""""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_SIGNALS_CHANNEL_ID):
        log.warning("Structure update skipped -- TELEGRAM_BOT_TOKEN/TELEGRAM_SIGNALS_CHANNEL_ID not configured.")
        return False

    now = dt.datetime.now()
    fingerprint = _structure_fingerprint(payload)
    if _is_duplicate(fingerprint, now=now, store=_last_structure_update_by_fingerprint):
        log.info(f"Structure update suppressed (duplicate within {DEDUP_WINDOW_SECONDS}s): {fingerprint}")
        return False

    if not _post_to_channel(_format_structure_update(payload)):
        return False

    _last_structure_update_by_fingerprint[fingerprint] = now
    log.info(
        "Structure update sent",
        extra={"symbol": payload.get("symbol"), "level": payload.get("level"),
               "current_role": payload.get("current_role")},
    )

    if chart_path:
        photo_sent = _post_photo_to_channel(chart_path)
        log.info(f"Structure chart {'sent' if photo_sent else 'send failed'}: {chart_path}")

    return True
