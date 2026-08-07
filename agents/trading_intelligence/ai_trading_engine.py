"""
agents/trading_intelligence/ai_trading_engine.py -- Module 3: AI Trading
Engine. "Generate: BUY CE, BUY PE, HOLD, NO TRADE. Every recommendation
must include: Confidence, Probability, Risk Score, Entry, Stop Loss,
Target, Reasoning."

BUY CE / BUY PE / NO_TRADE and Confidence/Entry/Target/SL/Reasoning are
NOT reimplemented here -- they come straight from oi_engine.generate_signal(),
the SAME rule-based signal function app.py's live dashboard already runs
every cycle (see oi_engine.py's own "Never duplicate this logic elsewhere"
rule). This module adds exactly three things generate_signal() doesn't
already produce:

1. HOLD -- a state that only makes sense once a position exists. Checked
   BEFORE ever calling generate_signal(): if this engine already has an
   open ti_paper_trades row for the symbol, the recommendation is HOLD
   (or the trade is closed, if target/SL has genuinely been hit) --
   never a fresh entry signal while a position is already open.

2. Probability -- a HONEST calibration, distinct from Confidence.
   Confidence (from generate_signal) is a same-cycle, rule-based estimate.
   Probability here is this engine's OWN historical win rate, bucketed by
   confidence range, from its OWN closed ti_paper_trades -- the same
   "bucketed win-rate calibration, insufficient-sample buckets flagged
   honestly rather than shown as a misleadingly precise percentage"
   pattern backtest.score_calibration_report() already established for
   the S/R engine, applied here to THIS engine's own trade history
   (which starts empty -- Probability is honestly None with a stated
   reason until enough trades accumulate, never fabricated).

3. Risk Score -- reuses agents.risk_manager.risk_engine's own primitives
   (position_sizing_check, value_at_risk, expected_shortfall) rather than
   inventing a second risk-scoring scheme; see _compute_risk_score()'s own
   docstring for exactly what it measures.
"""
import dataclasses
import datetime as dt

from oi_engine import detect_bias, generate_signal, oi_walls

from . import data_access, institutional_intelligence, market_data, ti_store

CALIBRATION_MIN_SAMPLE = 5
CALIBRATION_BUCKETS = ((0, 39), (40, 59), (60, 79), (80, 100))


@dataclasses.dataclass
class Recommendation:
    symbol: str
    action: str  # "BUY CE" | "BUY PE" | "HOLD" | "NO_TRADE"
    direction: str | None
    confidence: int | None
    probability: float | None
    probability_note: str
    risk_score: int | None
    entry_price: float | None
    sl_price: float | None
    target_price: float | None
    qty: int | None
    reasoning: str
    open_trade_id: int | None = None


def _calibrated_probability(confidence: int | None) -> tuple:
    """Returns (probability_pct: float|None, note: str). None with an
    honest note when there aren't enough of THIS engine's own closed
    trades in the matching confidence bucket yet -- never a guessed
    number standing in for real history."""
    if confidence is None:
        return None, "no confidence score to calibrate against"
    bucket = next((b for b in CALIBRATION_BUCKETS if b[0] <= confidence <= b[1]), None)
    if bucket is None:
        return None, "confidence out of expected range"
    trades = [
        t for t in ti_store.list_closed_trades(limit=10_000)
        if t.get("confidence") is not None and bucket[0] <= t["confidence"] <= bucket[1]
    ]
    if len(trades) < CALIBRATION_MIN_SAMPLE:
        return None, (
            f"insufficient history -- only {len(trades)} closed trade(s) in the "
            f"{bucket[0]}-{bucket[1]} confidence bucket (need >= {CALIBRATION_MIN_SAMPLE})"
        )
    wins = sum(1 for t in trades if (t.get("points") or 0) > 0)
    pct = round(wins / len(trades) * 100, 1)
    return pct, f"historical win rate across {len(trades)} closed trade(s) in this confidence bucket"


def _compute_risk_score(*, entry_price: float, sl_price: float, capital: float, risk_pct: float) -> int:
    """0-100, HIGHER = riskier (documented explicitly since "risk score"
    has no universal convention). Two real, transparent inputs:
    - Position sizing feasibility (agents.risk_manager.risk_engine.
      position_sizing_check): if the stop is too wide for the configured
      risk budget to size even 1 unit, that's a hard risk flag (60 pts).
    - Stop distance as a fraction of entry premium: a stop that's a large
      % of the premium itself (common for cheap OTM options) means a
      normal-looking price wobble can look like a full stop-out -- scaled
      contribution, capped at 40 pts."""
    from agents.risk_manager import risk_engine
    stop_points = abs(entry_price - sl_price)
    check = risk_engine.position_sizing_check(stop_points, capital=capital, risk_pct=risk_pct)
    score = 0 if check.passed else 60
    if entry_price > 0:
        stop_pct_of_premium = min(1.0, stop_points / entry_price)
        score += round(stop_pct_of_premium * 40)
    return max(0, min(100, score))


def _price_trend_pct(candles) -> float | None:
    """Recent underlying % change -- the same "cheap momentum proxy"
    oi_engine.detect_bias() itself documents needing, computed here from
    the last 5 candles of the already-archived data (never a live fetch)."""
    if candles.empty or len(candles) < 6:
        return None
    recent = candles.tail(6)
    start, end = recent.iloc[0]["close"], recent.iloc[-1]["close"]
    if not start:
        return None
    return round((end - start) / start * 100, 4)


def _check_open_trade_exit(trade: dict, snapshot) -> dict | None:
    """Returns a close instruction dict if target/SL has genuinely been
    hit against the CURRENT stored LTP for this trade's strike/direction,
    else None (still open -> HOLD). Never a live fetch -- the LTP comes
    from the same already-stored cycle market_data.get_snapshot() read."""
    row = next((r for r in snapshot.strikes if r.strike == trade["strike"]), None)
    if row is None:
        return None
    current_ltp = row.ce_ltp if trade["direction"] == "CE" else row.pe_ltp
    if not current_ltp or current_ltp <= 0:
        return None
    if trade["target_price"] and current_ltp >= trade["target_price"]:
        return {"exit_price": current_ltp, "exit_reason": "TARGET HIT"}
    if trade["sl_price"] and current_ltp <= trade["sl_price"]:
        return {"exit_price": current_ltp, "exit_reason": "STOP LOSS"}
    return None


def evaluate(symbol: str, *, capital: float = 500000.0, risk_pct: float = 1.0,
             expiry_date: dt.date | None = None) -> Recommendation:
    """The full Module 3 evaluation for one symbol. Never raises -- an
    unavailable snapshot degrades to a NO_TRADE recommendation with an
    honest reason, the same contract every data-reading function in this
    framework already holds to."""
    snapshot = market_data.get_snapshot(symbol, expiry_date=expiry_date)
    if not snapshot.available:
        return Recommendation(
            symbol=symbol, action="NO_TRADE", direction=None, confidence=None, probability=None,
            probability_note="no market data available", risk_score=None, entry_price=None, sl_price=None,
            target_price=None, qty=None, reasoning=snapshot.reason,
        )

    open_trades = ti_store.list_open_trades(symbol=symbol)
    if open_trades:
        trade = open_trades[0]
        exit_instruction = _check_open_trade_exit(trade, snapshot)
        if exit_instruction is not None:
            ti_store.close_trade(trade["id"], **exit_instruction)
            return Recommendation(
                symbol=symbol, action="NO_TRADE", direction=trade["direction"], confidence=trade["confidence"],
                probability=trade["probability"], probability_note="position just closed this cycle",
                risk_score=trade["risk_score"], entry_price=trade["entry_price"], sl_price=trade["sl_price"],
                target_price=trade["target_price"], qty=trade["qty"],
                reasoning=f"{trade['direction']} position closed: {exit_instruction['exit_reason']} at "
                          f"{exit_instruction['exit_price']}.",
                open_trade_id=None,
            )
        return Recommendation(
            symbol=symbol, action="HOLD", direction=trade["direction"], confidence=trade["confidence"],
            probability=trade["probability"], probability_note="from entry", risk_score=trade["risk_score"],
            entry_price=trade["entry_price"], sl_price=trade["sl_price"], target_price=trade["target_price"],
            qty=trade["qty"],
            reasoning=f"Holding open {trade['direction']} position from {trade['entry_price']} -- "
                      f"neither target ({trade['target_price']}) nor SL ({trade['sl_price']}) reached yet.",
            open_trade_id=trade["id"],
        )

    rows = snapshot.strikes
    atm, pcr, underlying = snapshot.atm, snapshot.pcr, snapshot.underlying_ltp
    if atm is None or pcr is None or not rows:
        return Recommendation(
            symbol=symbol, action="NO_TRADE", direction=None, confidence=None, probability=None,
            probability_note="no confidence to calibrate", risk_score=None, entry_price=None, sl_price=None,
            target_price=None, qty=None, reasoning="incomplete cycle data (missing ATM/PCR/strikes).",
        )

    candles = data_access.load_candles(symbol)
    price_trend_pct = _price_trend_pct(candles)
    market_structure = data_access.latest_market_structure(symbol)
    bias, note = detect_bias(rows, atm, pcr, price_trend_pct, underlying, market_structure)
    support, resistance = oi_walls(rows)
    signal = generate_signal(
        rows, atm, bias, note, pcr, support, resistance, underlying=underlying,
        expiry_date=expiry_date, market_structure=market_structure,
    )

    if signal["action"] != "BUY CE" and signal["action"] != "BUY PE":
        return Recommendation(
            symbol=symbol, action="NO_TRADE", direction=None, confidence=signal.get("confidence"),
            probability=None, probability_note="no trade to calibrate", risk_score=None, entry_price=None,
            sl_price=None, target_price=None, qty=None, reasoning=signal.get("reason", "no edge this cycle"),
        )

    probability, probability_note = _calibrated_probability(signal["confidence"])
    risk_score = _compute_risk_score(
        entry_price=signal["entry_price"], sl_price=signal["sl_price"], capital=capital, risk_pct=risk_pct,
    )

    import position_sizing
    qty = position_sizing.compute_quantity(
        signal["entry_price"], signal["sl_price"], sizing_mode="risk_pct",
        capital=capital, risk_pct=risk_pct, min_qty=0,
    )

    ii = institutional_intelligence.analyze(symbol, underlying=underlying, expiry_date=expiry_date)
    relevant = [f for f in ii.get("findings", []) if f.strike == signal["strike"]]
    reasoning = signal["reason"]
    if relevant:
        reasoning += " Also: " + "; ".join(f.description for f in relevant[:3])

    return Recommendation(
        symbol=symbol, action=signal["action"], direction=signal["direction"], confidence=signal["confidence"],
        probability=probability, probability_note=probability_note, risk_score=risk_score,
        entry_price=signal["entry_price"], sl_price=signal["sl_price"], target_price=signal["target_price"],
        qty=qty, reasoning=reasoning,
    )
