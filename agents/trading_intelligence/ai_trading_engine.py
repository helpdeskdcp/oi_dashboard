"""
agents/trading_intelligence/ai_trading_engine.py -- Module 3: AI Trading
Engine. "Generate: BUY CE, BUY PE, HOLD, NO TRADE." Extended for the
institutional-grade review pass (Priority 2) to also carry: Market Bias,
Multiple Targets (T1/T2/T3), Expected Move, Time Horizon, and a
structured reasoning breakdown (Institutional/OI/Greeks/Price Action) --
alongside the original Confidence/Probability/Risk Score/Entry/Stop
Loss/Reasoning.

BUY CE / BUY PE / NO_TRADE and Confidence/Entry/Target1/Reasoning are
NOT reimplemented here -- they come straight from oi_engine.generate_signal(),
the SAME rule-based signal function app.py's live dashboard already runs
every cycle (see oi_engine.py's own "Never duplicate this logic elsewhere"
rule). This module adds exactly what generate_signal() doesn't already
produce:

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
   reason until enough trades accumulate, never fabricated). Also
   distinct from strike_intelligence.py's own per-strike "AI Buy/Sell
   Probability" (an OI-positioning lean) and its "Probability of ITM"
   (risk-neutral options pricing) -- three different numbers, on purpose.

3. Risk Score -- reuses agents.risk_manager.risk_engine's own primitives
   (position_sizing_check) rather than inventing a second risk-scoring
   scheme; see _compute_risk_score()'s own docstring for exactly what it
   measures.

4. Targets T2/T3 -- generate_signal() only ever projects ONE target, off
   the single heaviest OI wall (walls[0]). T2/T3 extend the EXACT SAME
   projection formula (never a different methodology) onto the 2nd/3rd
   heaviest walls oi_engine.oi_walls() already returns, using the same
   delta_used generate_signal() already computed.

5. Expected Move -- reuses strike_intelligence.expected_move (the exact
   same spot*IV*sqrt(T) formula Module 2b already uses for its own
   per-strike field), never a second copy of this formula.

6. Structured reasoning -- institutional_reasoning/oi_reasoning/
   greeks_reasoning/price_action_reasoning break the same evidence
   signal["reason"] is already built from into labeled sections, for a
   caller (the dashboard) that wants to show WHY separately from WHAT.

Performance/modularity note (this review pass): evaluate() now accepts
an optional pre-fetched `snapshot` and `findings` so a caller that
already has both (api.get_symbol_overview(), the normal case) never
pays for a second cycles/strikes read or a second full institutional-
intelligence sweep for the same cycle -- see this module's own git
history for the "before" version, which always re-fetched both.
"""
import dataclasses
import datetime as dt

from oi_engine import detect_bias, generate_signal, oi_walls

from . import data_access, institutional_intelligence, market_data, strike_intelligence, ti_store

CALIBRATION_MIN_SAMPLE = 5
CALIBRATION_BUCKETS = ((0, 39), (40, 59), (60, 79), (80, 100))
MIN_TARGET_PCT = 0.15  # matches oi_engine.generate_signal's own default min_target_percent


@dataclasses.dataclass
class Recommendation:
    symbol: str
    action: str  # "BUY CE" | "BUY PE" | "HOLD" | "NO_TRADE"
    direction: str | None
    strike: int | None  # the strike this signal/trade is on -- paper_trading.py needs this
    # to open a ti_paper_trades row that _check_open_trade_exit() can actually match
    # against a future cycle's snapshot.strikes; None only for genuine NO_TRADE states.
    market_bias: str | None
    confidence: int | None
    probability: float | None
    probability_note: str
    risk_score: int | None
    entry_price: float | None
    sl_price: float | None
    target_price: float | None  # T1 -- kept for backward compatibility with the pre-review field name
    targets: list  # [T1, T2, T3], fewer if the chain has fewer walls, [] if no trade
    expected_move_pts: float | None
    time_horizon: str
    qty: int | None
    reasoning: str
    institutional_reasoning: str
    oi_reasoning: str
    greeks_reasoning: str
    price_action_reasoning: str
    open_trade_id: int | None = None


def _no_trade(symbol: str, *, reason: str, market_bias: str | None = None, direction: str | None = None,
              strike: int | None = None, confidence: int | None = None, oi_reasoning: str = "") -> Recommendation:
    """Every "nothing to do here" path (no data, neutral bias, a position
    that just closed) shares this one constructor -- removes what was,
    before this review, five separately hand-written NO_TRADE
    Recommendation(...) calls each listing all 20 fields."""
    return Recommendation(
        symbol=symbol, action="NO_TRADE", direction=direction, strike=strike, market_bias=market_bias,
        confidence=confidence, probability=None, probability_note="no trade to calibrate", risk_score=None,
        entry_price=None, sl_price=None, target_price=None, targets=[], expected_move_pts=None,
        time_horizon="n/a", qty=None, reasoning=reason, institutional_reasoning="", oi_reasoning=oi_reasoning,
        greeks_reasoning="", price_action_reasoning="",
    )


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


def _multi_targets(signal: dict, support: list, resistance: list, atm: float) -> list:
    """T1 is ALWAYS generate_signal()'s own target_price -- the SAME value
    kept in Recommendation.target_price (the field _check_open_trade_exit()
    actually watches to close a paper trade), so the two never disagree.
    T2/T3 extend the EXACT SAME projection formula onto the 2nd/3rd
    heaviest walls -- never a different targeting methodology mixed in --
    but are only appended when they're strictly further than the target
    before them (oi_walls() orders by OI weight, not by distance, so a
    wall ranked 2nd/3rd by weight can project a NEARER price than T1; such
    a candidate is dropped rather than reordered ahead of T1, since T1
    must stay anchored to target_price)."""
    entry_price, delta_used = signal["entry_price"], signal["delta_used"]
    walls = resistance if signal["direction"] == "CE" else support
    targets = [signal["target_price"]]
    for wall in walls[1:3]:
        underlying_move = abs(wall.strike - atm)
        projected_move = underlying_move * delta_used
        t = round(entry_price + max(projected_move, entry_price * MIN_TARGET_PCT), 2)
        if t > targets[-1]:
            targets.append(t)
    return targets[:3]


def _time_horizon(expiry_date: dt.date | None) -> str:
    """A real, honest label from real days-to-expiry -- never a vague
    "short term"/"long term" guess."""
    if expiry_date is None:
        return "unknown (no expiry date provided)"
    days = (expiry_date - dt.date.today()).days
    if days <= 0:
        return "Intraday (expires today)"
    if days <= 2:
        return f"{days} day(s) to expiry -- near-term"
    if days <= 7:
        return f"{days} day(s) to expiry -- short swing"
    return f"{days} day(s) to expiry -- extended swing"


def _reasoning_sections(*, signal: dict, pcr: float, findings: list, atm_row, price_trend_pct: float | None,
                         market_structure: dict | None) -> dict:
    """Splits the SAME evidence signal["reason"]/pcr/findings/atm_row/
    price_trend_pct/market_structure are already built from into four
    labeled sections -- never a second computation of any of these
    values, only a second (English-language) DESCRIPTION of ones that
    already exist."""
    oi_bits = [f"PCR {pcr}", f"ATM signal: {getattr(atm_row, signal['direction'].lower() + '_signal', 'n/a')}"]
    oi_reasoning = "; ".join(oi_bits) + f". {signal.get('reason', '')}"

    delta_used, delta_source = signal.get("delta_used"), signal.get("delta_source", "n/a")
    greeks_reasoning = (
        f"Delta used: {delta_used:.3f} ({delta_source})." if delta_used is not None else "Delta unavailable this cycle."
    )

    if price_trend_pct is not None:
        price_action_reasoning = f"Underlying moved {price_trend_pct:+.3f}% over the last 6 archived candles."
    else:
        price_action_reasoning = "Not enough recent candle history to read short-term price action yet."
    if market_structure and market_structure.get("regime") not in (None, "UNKNOWN"):
        price_action_reasoning += f" Market regime: {market_structure['regime']} (ADX={market_structure.get('adx')})."

    relevant = [f for f in findings if f.strike == signal["strike"]]
    institutional_reasoning = (
        "; ".join(f.description for f in relevant[:3]) if relevant
        else "No institutional-intelligence findings at this strike this cycle."
    )

    return {
        "oi_reasoning": oi_reasoning, "greeks_reasoning": greeks_reasoning,
        "price_action_reasoning": price_action_reasoning, "institutional_reasoning": institutional_reasoning,
    }


def evaluate(symbol: str, *, snapshot=None, findings: list | None = None, capital: float = 500000.0,
             risk_pct: float = 1.0, expiry_date: dt.date | None = None) -> Recommendation:
    """The full Module 3 evaluation for one symbol. Never raises -- an
    unavailable snapshot degrades to a NO_TRADE recommendation with an
    honest reason, the same contract every data-reading function in this
    framework already holds to.

    `snapshot`/`findings`: pass these when the caller already has them
    this cycle (api.get_symbol_overview() does) to skip a second
    cycles/strikes read and a second full institutional-intelligence
    sweep. Standalone callers (every test here, a future scheduler cycle)
    leave both None and get fresh ones, unchanged from before this review."""
    snapshot = snapshot or market_data.get_snapshot(symbol, expiry_date=expiry_date)
    if not snapshot.available:
        return _no_trade(symbol, reason=snapshot.reason)

    rows, atm, pcr, underlying = snapshot.strikes, snapshot.atm, snapshot.pcr, snapshot.underlying_ltp
    candles = data_access.load_candles(symbol)
    price_trend_pct = _price_trend_pct(candles)
    market_structure = data_access.latest_market_structure(symbol)
    market_bias, bias_note = None, ""
    if atm is not None and pcr is not None and rows:
        market_bias, bias_note = detect_bias(rows, atm, pcr, price_trend_pct, underlying, market_structure)

    open_trades = ti_store.list_open_trades(symbol=symbol)
    if open_trades:
        trade = open_trades[0]
        exit_instruction = _check_open_trade_exit(trade, snapshot)
        if exit_instruction is not None:
            ti_store.close_trade(trade["id"], **exit_instruction)
            return _no_trade(
                symbol, direction=trade["direction"], strike=trade["strike"], confidence=trade["confidence"],
                market_bias=market_bias,
                reason=f"{trade['direction']} position closed: {exit_instruction['exit_reason']} at "
                       f"{exit_instruction['exit_price']}.",
            )
        return Recommendation(
            symbol=symbol, action="HOLD", direction=trade["direction"], strike=trade["strike"],
            market_bias=market_bias,
            confidence=trade["confidence"], probability=trade["probability"], probability_note="from entry",
            risk_score=trade["risk_score"], entry_price=trade["entry_price"], sl_price=trade["sl_price"],
            target_price=trade["target_price"], targets=[trade["target_price"]] if trade["target_price"] else [],
            expected_move_pts=None, time_horizon=_time_horizon(expiry_date), qty=trade["qty"],
            reasoning=f"Holding open {trade['direction']} position from {trade['entry_price']} -- "
                      f"neither target ({trade['target_price']}) nor SL ({trade['sl_price']}) reached yet.",
            institutional_reasoning="", oi_reasoning="", greeks_reasoning="", price_action_reasoning="",
            open_trade_id=trade["id"],
        )

    if atm is None or pcr is None or not rows:
        return _no_trade(symbol, reason="incomplete cycle data (missing ATM/PCR/strikes).")

    support, resistance = oi_walls(rows)
    signal = generate_signal(
        rows, atm, market_bias, bias_note, pcr, support, resistance, underlying=underlying,
        expiry_date=expiry_date, market_structure=market_structure,
    )
    if signal["action"] not in ("BUY CE", "BUY PE"):
        return _no_trade(
            symbol, market_bias=market_bias, confidence=signal.get("confidence"),
            reason=signal.get("reason", "no edge this cycle"),
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

    if findings is None:
        findings = institutional_intelligence.analyze(
            symbol, snapshot=snapshot, underlying=underlying, expiry_date=expiry_date,
        ).get("findings", [])

    atm_row = next((r for r in rows if r.strike == atm), None)
    sections = _reasoning_sections(
        signal=signal, pcr=pcr, findings=findings, atm_row=atm_row, price_trend_pct=price_trend_pct,
        market_structure=market_structure,
    )
    iv_for_move = (atm_row.ce_iv if signal["direction"] == "CE" else atm_row.pe_iv) if atm_row else None

    return Recommendation(
        symbol=symbol, action=signal["action"], direction=signal["direction"], strike=signal["strike"],
        market_bias=market_bias,
        confidence=signal["confidence"], probability=probability, probability_note=probability_note,
        risk_score=risk_score, entry_price=signal["entry_price"], sl_price=signal["sl_price"],
        target_price=signal["target_price"], targets=_multi_targets(signal, support, resistance, atm),
        expected_move_pts=strike_intelligence.expected_move(underlying, iv_for_move, expiry_date),
        time_horizon=_time_horizon(expiry_date), qty=qty, reasoning=signal["reason"], **sections,
    )
