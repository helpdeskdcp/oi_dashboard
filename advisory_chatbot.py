"""
advisory_chatbot.py -- Interactive advisory chatbot (informational ONLY).

Extends the existing chatgpt_commentary.py pattern (same OpenAI setup, same
safety philosophy) into genuine Q&A: the trader asks a question, the bot
answers using ONLY the structured data we hand it as context.

HARD SAFETY BOUNDARY (by design, not just prompt-engineering):
- This module has NO access to any order-placement/modification/closing
  function. It literally cannot execute a trade -- there's no code path
  from here to AngelOneFetcher's (nonexistent) order-methods. Even if the
  LLM "wanted" to place a trade, there's nothing here that could do it.
- The system-prompt ALSO explicitly instructs the model to never claim it
  can act, and to always hedge/cite its data -- defense in depth, not the
  only safeguard.

Fully optional: if OPENAI_API_KEY isn't set, this silently reports itself
disabled -- same graceful-degradation pattern as chatgpt_commentary.py.
"""
import os
import logging

log = logging.getLogger("advisory_chatbot")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CHATBOT_ENABLED = bool(OPENAI_API_KEY)

SYSTEM_PROMPT = """You are an advisory assistant for IDaddy AI, a rule-based options-trading
analytics dashboard for the Indian market (NSE/BSE/MCX). A trader is asking you a question;
answer it using ONLY the structured data provided to you as context below their question.

ANSWER THE ACTUAL QUESTION FIRST. If they ask about support/resistance, OI walls, PCR, or
today's setup for a symbol, look at the "Live symbol snapshot" section and give them the
actual numbers (V1/V2 resistance, V1/V2 support, trend, signal, PCR) for that symbol. Do
NOT deflect to a generic accuracy/calibration disclaimer instead of answering -- that is
only relevant when they specifically ask about accuracy, win-rate, or reliability.

HARD RULES (never break these):
1. You CANNOT place, modify, or close any trade -- you have no such capability at all. If
   asked to "buy", "sell", "execute", "close this position", etc., clearly explain that you
   are informational-only and the trader must act themselves through their broker.
2. Only describe what the given data shows. NEVER invent a price, OI value, level, or
   statistic that isn't in the context you were given. If the specific symbol/data they
   asked about genuinely ISN'T in your context, say so plainly and name what IS available
   instead -- do not guess or extrapolate.
3. NEVER claim certainty about future price direction. Use hedged language ("could",
   "suggests", "if X holds") never "will" or "guaranteed") -- but this does NOT mean
   refusing to state the actual levels/numbers you DO have; hedge the INTERPRETATION, not
   the facts.
4. ALWAYS state which data/engine you're basing an answer on (e.g. "based on Engine V2's
   support level of 24,150..."). If the symbol snapshot is marked "LAST KNOWN" rather than
   "LIVE", mention that briefly (e.g. "as of the last logged data, before market close...")
   so the trader knows it may not reflect the current live price.
5. ONLY bring up accuracy/win-rate/calibration numbers when the trader explicitly asks about
   accuracy, win-rate, reliability, or "is this profitable". Do NOT proactively insert this
   caveat into answers about levels, setups, or signals unless asked. When you DO discuss
   accuracy, be completely honest -- never oversell, and say plainly if the sample is small
   or the numbers are weak.
6. NEVER use the words "profitable", "guaranteed", or "sure thing" to describe any signal,
   level, or strategy. Frame everything as analytical information for the trader's OWN
   decision -- not a recommendation to act.
7. Keep answers concise (3-6 sentences) and in the same casual Hindi/English mix tone as
   the rest of the dashboard, unless the question is purely technical/English.
"""


def _build_context_block(context_data: dict) -> str:
    """Formats the assembled data (positions, S/R-levels, calibration, etc.)
    into a plain-text context block for the model."""
    lines = ["=== CURRENT DASHBOARD DATA (use ONLY this -- do not invent anything else) ==="]

    positions = context_data.get("open_positions") or []
    if positions:
        lines.append(f"\nOpen positions ({len(positions)}):")
        for p in positions:
            lines.append(f"  - {p.get('symbol')}: entry={p.get('entry_price')}, "
                         f"LTP={p.get('current_ltp')}, PnL={p.get('current_pnl')} ({p.get('current_pnl_pct')}%), "
                         f"suggested SL={p.get('sl_price')}, suggested target={p.get('target_price')}")
    else:
        lines.append("\nOpen positions: none currently open.")

    symbols_summary = context_data.get("symbols_summary") or []
    if symbols_summary:
        lines.append(f"\nLive symbol snapshot ({len(symbols_summary)} symbols):")
        for s in symbols_summary:
            freshness = "LIVE (current)" if s.get("is_live") else f"LAST KNOWN (as of {s.get('as_of', 'unknown time')} -- market may be closed or data not yet refreshed this session)"
            lines.append(f"  - {s.get('symbol')} (ATM strike: {s.get('atm_strike')}) [{freshness}]: LTP={s.get('ltp')}, PCR={s.get('pcr')}")
            lines.append(f"      V1 levels: Resistance={s.get('v1_resistance')}, Resistance-Reversal={s.get('v1_resistance_reversal')}, "
                         f"Support={s.get('v1_support')}, Support-Reversal={s.get('v1_support_reversal')}")
            lines.append(f"      V2 (experimental) levels: Resistance={s.get('v2_resistance')}, Support={s.get('v2_support')}, "
                         f"Trend={s.get('v2_trend')}, Signal={s.get('v2_signal')} (reason: {s.get('v2_signal_reason')})")
            lines.append(f"      OI walls: CE wall at strike {s.get('ce_oi_wall_strike')} (OI={s.get('ce_oi_wall_value')}), "
                         f"PE wall at strike {s.get('pe_oi_wall_strike')} (OI={s.get('pe_oi_wall_value')})")

    calibration = context_data.get("calibration")
    if calibration:
        lines.append(f"\nCalibration/track-record (from {calibration.get('total_trades', 0)} actually-closed trades):")
        for tier, d in (calibration.get("by_tier") or {}).items():
            lines.append(f"  - {tier} tier: {d.get('trades')} trades, {d.get('win_rate')}% win-rate"
                         f"{' (sample too small to be meaningful)' if not d.get('sufficient_sample') else ''}")
    else:
        lines.append("\nCalibration/track-record: not enough closed trades yet for meaningful stats.")

    return "\n".join(lines)


def ask_advisor(question: str, context_data: dict, conversation_history=None):
    """
    Answers a trader's question using the provided context-data.

    question: the trader's typed question (plain text)
    context_data: {"open_positions": [...], "symbols_summary": [...], "calibration": {...}}
                   -- all assembled from OUR OWN already-computed live data, never invented.
    conversation_history: optional list of {"role": "user"|"assistant", "content": str}
                           from earlier in the SAME chat session, for follow-up questions.

    Returns {"answer": str, "enabled": bool} -- if disabled, answer explains why.
    """
    if not CHATBOT_ENABLED:
        return {"answer": "Advisory chatbot is not configured -- OPENAI_API_KEY isn't set in .env. "
                          "This is fully optional; the dashboard's core signals/trading logic don't need it.",
                "enabled": False}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        context_block = _build_context_block(context_data)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in (conversation_history or [])[-6:]:   # cap history to keep context-size reasonable
            messages.append(turn)
        messages.append({"role": "user", "content": f"{question}\n\n{context_block}"})

        resp = client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, max_tokens=350, temperature=0.4,
        )
        return {"answer": resp.choices[0].message.content.strip(), "enabled": True}
    except Exception as e:
        log.warning(f"Advisory chatbot failed: {e}")
        return {"answer": f"Sorry, I couldn't process that right now ({e}). Try again in a moment.", "enabled": True}
