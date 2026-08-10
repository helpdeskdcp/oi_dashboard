#!/usr/bin/env python3
"""
oi_engine.py -- Shared, side-effect-free OI analysis + signal logic.

CRITICAL: both the live dashboard (app.py) AND the backtester (backtest.py)
import from this file. Never duplicate this logic elsewhere -- if live and
backtest ever compute bias/signals differently, backtest results become
meaningless (they'd be testing a different strategy than what's live).
"""

from dataclasses import dataclass
from greeks import black_scholes_greeks, time_to_expiry_years
from market_structure import nearest_major_level, cross_verify_wall


@dataclass
class StrikeRow:
    strike: int
    ce_oi: int = 0
    ce_oi_chg: int = 0
    ce_vol: int = 0
    ce_ltp: float = 0.0
    ce_chg_pct: float = 0.0
    ce_signal: str = "Neutral"
    ce_iv: float = 0.0
    ce_delta: float = 0.0
    ce_gamma: float = 0.0
    ce_theta: float = 0.0
    ce_vega: float = 0.0
    ce_buy_qty: int = 0
    ce_sell_qty: int = 0
    pe_oi: int = 0
    pe_oi_chg: int = 0
    pe_vol: int = 0
    pe_ltp: float = 0.0
    pe_chg_pct: float = 0.0
    pe_signal: str = "Neutral"
    pe_iv: float = 0.0
    pe_delta: float = 0.0
    pe_gamma: float = 0.0
    pe_theta: float = 0.0
    pe_vega: float = 0.0
    pe_buy_qty: int = 0
    pe_sell_qty: int = 0


def classify_buildup(price_chg_pct, oi_chg, min_oi_chg_threshold=0, prev_oi=None, min_oi_chg_pct=None,
                      min_price_chg_pct=0):
    """
    Two noise-filter modes, combinable:
    - min_oi_chg_threshold: absolute contract-count floor (works fine for one
      instrument, but a single global value is wrong across instruments of very
      different OI scale -- 500 contracts is noise for NIFTY, might be most of
      NATGASMINI's entire OI).
    - min_oi_chg_pct + prev_oi: percentage-based -- ignores changes smaller than
      min_oi_chg_pct% of the PREVIOUS OI at that strike. Scales correctly
      regardless of instrument size. Preferred when prev_oi is known.

    min_price_chg_pct: on EXPIRY DAY, theta decay alone can crash an option's
    premium even with flat OI and no genuine writing/unwinding happening --
    "premium fell" != "someone sold". Caller should pass a higher threshold
    (e.g. 3-5%) on expiry day so small price moves (likely just decay) don't
    get misread as Long Unwinding/Short Buildup. Default 0 = no change from
    normal-day behavior.
    """
    if min_oi_chg_pct is not None and prev_oi and prev_oi > 0:
        pct_change = abs(oi_chg) / prev_oi * 100
        if pct_change < min_oi_chg_pct:
            return "Neutral"
    elif abs(oi_chg) < min_oi_chg_threshold:
        return "Neutral"
    if abs(price_chg_pct) < min_price_chg_pct:
        return "Neutral"   # price move too small to trust on expiry day -- likely just theta
    if oi_chg > 0 and price_chg_pct > 0:
        return "Long Buildup"
    if oi_chg > 0 and price_chg_pct < 0:
        return "Short Buildup"
    if oi_chg < 0 and price_chg_pct > 0:
        return "Short Covering"
    if oi_chg < 0 and price_chg_pct < 0:
        return "Long Unwinding"
    return "Neutral"


# Standard bullish/bearish lean per classification, applied to CE vs PE
# (mirrored -- puts profit from the opposite direction to calls). See
# module-level notes: this is the standard Sensibull/Opstra-style mapping,
# not a new invented scoring scheme.
_CE_BUILDUP_LEAN = {"Long Buildup": 1, "Short Buildup": -1, "Short Covering": 1, "Long Unwinding": -1, "Neutral": 0}
_PE_BUILDUP_LEAN = {"Long Buildup": -1, "Short Buildup": 1, "Short Covering": -1, "Long Unwinding": 1, "Neutral": 0}


def net_oi_buildup_lean(ce_signal, pe_signal):
    """
    Combines the ALREADY-computed ce_signal/pe_signal (from classify_buildup,
    which already runs live every cycle for every strike -- see
    row.ce_signal/row.pe_signal) into a single net bullish/bearish lean for
    one strike (typically the ATM strike).

    Returns {"ce_lean": -1|0|1, "pe_lean": -1|0|1, "net_lean": -2..2, "overall": "BULLISH"|"BEARISH"|"NEUTRAL"}
    """
    ce_lean = _CE_BUILDUP_LEAN.get(ce_signal, 0)
    pe_lean = _PE_BUILDUP_LEAN.get(pe_signal, 0)
    net_lean = ce_lean + pe_lean
    overall = "BULLISH" if net_lean > 0 else ("BEARISH" if net_lean < 0 else "NEUTRAL")
    return {"ce_lean": ce_lean, "pe_lean": pe_lean, "net_lean": net_lean, "overall": overall}


def compute_conviction_strength(ce_signal, pe_signal, pcr_history, current_pcr, ce_vol_history, pe_vol_history, current_ce_vol, current_pe_vol):
    """
    OI-Buildup Conviction-Strength: an HONEST count of how many independent
    factors genuinely agree on a bullish/bearish direction -- NOT a
    magnitude or price-target prediction (which we deliberately do not
    attempt; see project notes on avoiding overclaiming).

    4 independent factors checked:
      1. CE-OI-buildup lean (from classify_buildup's already-computed ce_signal)
      2. PE-OI-buildup lean (from pe_signal)
      3. PCR-trend (rising -> bullish-lean, falling -> bearish-lean)
      4. Volume-confirmation (genuine expansion on the side agreeing with
         the OI-buildup direction, using the SAME compute_volume_expansion
         check already used by the fake-breakout filter)

    Returns:
        {
            "score": 0-4 (how many factors agree with the DOMINANT direction),
            "direction": "BULLISH" | "BEARISH" | "NEUTRAL" (if no lean at all),
            "factors": [{"name": .., "agrees": bool, "detail": str}, ...],
        }
    """
    buildup = net_oi_buildup_lean(ce_signal, pe_signal)
    factors = []
    from sr_probability_engine import compute_volume_expansion

    # Factor 1 & 2: CE and PE buildup leans (already computed)
    factors.append({"name": "CE OI-Buildup", "lean": buildup["ce_lean"],
                     "detail": ce_signal or "Neutral"})
    factors.append({"name": "PE OI-Buildup", "lean": buildup["pe_lean"],
                     "detail": pe_signal or "Neutral"})

    # Factor 3: PCR-trend (rising PCR -> more put-buying/writing -> bullish-lean per convention;
    # falling PCR -> bearish-lean). Needs at least 3 prior PCR readings to judge a genuine trend.
    pcr_values = [p for p in (pcr_history or []) if p is not None]
    pcr_lean = 0
    pcr_detail = "Not enough history yet"
    if len(pcr_values) >= 3 and current_pcr is not None:
        recent_avg = sum(pcr_values[-3:]) / 3
        if current_pcr > recent_avg * 1.02:
            pcr_lean, pcr_detail = 1, f"Rising (current={current_pcr}, recent-avg={round(recent_avg,3)})"
        elif current_pcr < recent_avg * 0.98:
            pcr_lean, pcr_detail = -1, f"Falling (current={current_pcr}, recent-avg={round(recent_avg,3)})"
        else:
            pcr_detail = f"Flat (current={current_pcr}, recent-avg={round(recent_avg,3)})"
    factors.append({"name": "PCR Trend", "lean": pcr_lean, "detail": pcr_detail})

    # Factor 4: Volume-confirmation on whichever side (CE or PE) the OI-buildup favors
    vol_lean = 0
    vol_detail = "Not enough history yet"
    if buildup["net_lean"] != 0:
        if buildup["net_lean"] > 0:   # bullish lean -- check CE volume (buying activity)
            expanded, ratio = compute_volume_expansion(ce_vol_history, current_ce_vol)
        else:
            expanded, ratio = compute_volume_expansion(pe_vol_history, current_pe_vol)
        if expanded is True:
            vol_lean = 1 if buildup["net_lean"] > 0 else -1
            vol_detail = f"Genuine volume expansion ({ratio}x recent average)"
        elif expanded is False:
            vol_detail = f"Volume NOT expanded ({ratio}x recent average) -- weak confirmation"
        # expanded is None -> insufficient history, vol_lean stays 0 (not disqualifying, just uncounted)
    factors.append({"name": "Volume Confirmation", "lean": vol_lean, "detail": vol_detail})

    total_lean = sum(f["lean"] for f in factors)
    direction = "BULLISH" if total_lean > 0 else ("BEARISH" if total_lean < 0 else "NEUTRAL")
    # Score = how many factors AGREE with the dominant direction (not raw sum, so opposing factors don't cancel silently)
    if direction == "BULLISH":
        score = sum(1 for f in factors if f["lean"] > 0)
    elif direction == "BEARISH":
        score = sum(1 for f in factors if f["lean"] < 0)
    else:
        score = 0

    for f in factors:
        f["agrees"] = (f["lean"] > 0 and direction == "BULLISH") or (f["lean"] < 0 and direction == "BEARISH")

    return {"score": score, "direction": direction, "factors": factors}


def find_atm(underlying, step):
    return int(round(underlying / step) * step)


def wanted_strikes(underlying, step, strikes_each_side=4):
    atm = find_atm(underlying, step)
    ce = [atm + i * step for i in range(1, strikes_each_side + 1)]
    pe = [atm - i * step for i in range(1, strikes_each_side + 1)]
    return sorted(set([atm] + ce + pe)), atm


def calc_pcr(rows):
    total_ce = sum(r.ce_oi for r in rows) or 1
    total_pe = sum(r.pe_oi for r in rows)
    return round(total_pe / total_ce, 3)


def calc_max_pain(rows):
    best, min_pain = None, None
    for c in rows:
        pain = 0
        for r in rows:
            if r.strike <= c.strike:
                pain += r.ce_oi * max(0, c.strike - r.strike)
            if r.strike >= c.strike:
                pain += r.pe_oi * max(0, r.strike - c.strike)
        if min_pain is None or pain < min_pain:
            min_pain, best = pain, c.strike
    return best or 0


def oi_walls(rows):
    resistance = sorted(rows, key=lambda r: r.ce_oi, reverse=True)[:3]
    support = sorted(rows, key=lambda r: r.pe_oi, reverse=True)[:3]
    return support, resistance


def assess_market_quality(strikes) -> str:
    """Milestone 14 observability pass: a read-only liquidity read on the
    current option chain -- "NO_LIQUIDITY" (every strike genuinely empty),
    "THIN" (some activity but below a real-participation floor), or
    "NORMAL". `strikes` are StrikeRow instances (attribute access, not
    dict .get() -- this is the exact same type oi_walls()/detect_bias()
    above already take), so this reads ce_oi/pe_oi/ce_vol/pe_vol as
    attributes with `or 0` guarding the (never-actually-None, but
    defensive) case a field comes back None rather than the dataclass's
    own int default. Never used as a signal/bias input itself -- see
    that guarantee's own callers (intelligence_orchestrator.py,
    agents/intelligence_alerts/rules.py) for why: this is context for a
    READER to interpret a flat reading with, not a new input to
    detect_bias()/generate_signal()'s own directional logic."""
    total_oi = sum((r.ce_oi or 0) + (r.pe_oi or 0) for r in strikes)
    total_vol = sum((r.ce_vol or 0) + (r.pe_vol or 0) for r in strikes)

    if total_oi == 0:
        return "NO_LIQUIDITY"
    if total_oi < 1000 or total_vol < 100:
        return "THIN"
    return "NORMAL"


def detect_bias(rows, atm, pcr, price_trend_pct=None, underlying=None, market_structure=None):
    """
    price_trend_pct: recent underlying % change over the last few cycles (a cheap
    momentum proxy until proper candle/ATR infrastructure exists). PCR alone must
    NEVER set a directional bias -- it's context, not a trigger (this was a real
    bug: PCR>1.3 used to set BULLISH BIAS with zero price confirmation, meaning a
    heavy-put-writing day with a FALLING price could still show bullish). Now a
    PCR extreme only becomes a directional bias when price is actually moving the
    same way; otherwise it's flagged as an unconfirmed watch-only note.

    underlying + market_structure (with atr_14): used to make the price-trend
    confirmation threshold ATR-aware instead of a flat 0.05% -- a 0.05% wiggle
    is real noise for a low-ATR instrument (e.g. NATURALGAS barely moving) but
    might be too loose a bar for a genuinely volatile one. Falls back to the
    flat 0.05% when ATR data isn't available yet (e.g. first day of collection).
    """
    min_trend_pct = 0.05
    if underlying and market_structure and market_structure.get("atr_14"):
        atr_pct_of_price = market_structure["atr_14"] / underlying * 100
        min_trend_pct = max(0.05, atr_pct_of_price * 0.15)   # never looser than the 0.05% baseline

    near_res = min([r for r in rows if r.strike > atm], key=lambda r: r.strike, default=None)
    near_sup = min([r for r in rows if r.strike < atm], key=lambda r: atm - r.strike, default=None)
    atm_row = next((r for r in rows if r.strike == atm), None)

    ce_writing = near_res and near_res.ce_signal == "Short Buildup"
    pe_writing = near_sup and near_sup.pe_signal == "Short Buildup"
    ce_unwinding = near_res and near_res.ce_signal in ("Short Covering", "Long Unwinding")
    pe_unwinding = near_sup and near_sup.pe_signal in ("Short Covering", "Long Unwinding")

    # Direct ATM-strike rule: spot's position relative to ATM + raw ATM Call OI
    # direction (not price-classified buildup type -- just genuine OI increase/
    # decrease at the money). Independent, direct signal -- doesn't need PCR.
    atm_bullish = underlying is not None and atm_row and underlying > atm and atm_row.ce_oi_chg < 0
    atm_bearish = underlying is not None and atm_row and underlying < atm and atm_row.ce_oi_chg > 0

    bias, note = "NEUTRAL / RANGE", "OI build-up balanced dono taraf, breakout ka wait karo."

    if ce_writing and pe_writing:
        # Both sides writing simultaneously -- classic expiry pinning setup, not
        # a directional signal. Checked FIRST since it overrides single-side reads.
        bias, note = "PINNING / RANGE (THETA RISK)", (
            f"{near_res.strike} CE aur {near_sup.strike} PE dono pe fresh writing ho rahi hai — "
            f"market pin/range-bound ho sakta hai in dono strikes ke beech. Options premium "
            f"tezi se ghatega (theta decay) chahe direction kuch bhi ho -- naya directional "
            f"trade abhi risky hai."
        )
    elif ce_writing and pe_unwinding and pcr < 1.0:
        bias, note = "REVERSAL DOWN RISK", (
            f"{near_res.strike} CE pe fresh writing + {near_sup.strike} PE unwinding (dono confirm karte hain) "
            f"— upside capped, downside pressure genuinely strong lag raha hai."
        )
    elif pe_writing and ce_unwinding and pcr > 1.0:
        bias, note = "REVERSAL UP RISK", (
            f"{near_sup.strike} PE pe fresh writing + {near_res.strike} CE unwinding (dono confirm karte hain) "
            f"— downside capped, upside pressure genuinely strong lag raha hai."
        )
    elif atm_bullish:
        bias, note = "BULLISH ATM SIGNAL", (
            f"Spot ({underlying}) ATM ({atm}) se upar hai + ATM Call OI ghat raha hai "
            f"(chg {atm_row.ce_oi_chg}, Call Unwinding) — bullish signal, BUY CE bias."
        )
    elif atm_bearish:
        bias, note = "BEARISH ATM SIGNAL", (
            f"Spot ({underlying}) ATM ({atm}) se neeche hai + ATM Call OI badh raha hai "
            f"(chg {atm_row.ce_oi_chg}, Fresh Call Writing) — bearish signal, BUY PE bias."
        )
    elif near_res and near_res.ce_signal == "Short Buildup" and pcr < 1.0:
        bias, note = "REVERSAL DOWN RISK", f"{near_res.strike} CE pe fresh writing — upside capped lag sakta hai."
    elif near_sup and near_sup.pe_signal == "Short Buildup" and pcr > 1.0:
        bias, note = "REVERSAL UP RISK", f"{near_sup.strike} PE pe fresh writing — downside capped lag sakta hai."
    elif near_res and near_res.ce_signal == "Short Covering":
        bias, note = "BULLISH BREAKOUT", f"{near_res.strike} CE unwinding — upar breakout momentum."
    elif near_sup and near_sup.pe_signal == "Short Covering":
        bias, note = "BEARISH BREAKDOWN", f"{near_sup.strike} PE unwinding — neeche breakdown momentum."
    elif pcr > 1.5:
        # PCR thresholds: <0.7 bearish zone, >1.5 bullish zone, 0.7-1.5 (including
        # the explicit 0.7-1.2 "neutral" band) needs price-action confirmation
        # before any bias is set -- PCR alone is context, never a standalone trigger.
        if price_trend_pct is not None and price_trend_pct > min_trend_pct:
            bias, note = "BULLISH BIAS", f"PCR {pcr} (puts heavy, >1.5) + price trending up {price_trend_pct:+.2f}% (need >{min_trend_pct:.2f}%) — this cycle aligned (still needs 60s persistence + structural-level check before it's tradeable)."
        else:
            note = f"PCR {pcr} hai (puts heavy likhe ja rahe) lekin price confirmation nahi mila abhi (need >{min_trend_pct:.2f}% move) -- watch only, trade nahi."
    elif pcr < 0.7:
        if price_trend_pct is not None and price_trend_pct < -min_trend_pct:
            bias, note = "BEARISH BIAS", f"PCR {pcr} (calls heavy, <0.7) + price trending down {price_trend_pct:+.2f}% (need >{min_trend_pct:.2f}%) — this cycle aligned (still needs 60s persistence + structural-level check before it's tradeable)."
        else:
            note = f"PCR {pcr} hai (calls heavy likhe ja rahe) lekin price confirmation nahi mila abhi (need >{min_trend_pct:.2f}% move) -- watch only, trade nahi."
    elif 0.7 <= pcr <= 1.2:
        note = f"PCR {pcr} neutral zone (0.7-1.2) mein hai -- sirf Price Action confirmation ke baad hi trade lena, koi bias nahi diya ja raha abhi."
    return bias, note


BIAS_LEAN = {
    "BULLISH BREAKOUT": 20, "BULLISH ATM SIGNAL": 16, "BULLISH BIAS": 12, "REVERSAL UP RISK": 8,
    "PINNING / RANGE (THETA RISK)": -5, "NEUTRAL / RANGE": 0,
    "REVERSAL DOWN RISK": -8, "BEARISH BIAS": -12, "BEARISH ATM SIGNAL": -16, "BEARISH BREAKDOWN": -20,
}


def compute_trend_meter(bias: str, pcr: float, signal: dict, market_structure: dict = None):
    """
    Composite 0-100 'odometer' score for a single-glance trend gauge:
      0   = strong bearish (far left)
      50  = neutral / dead center
      100 = strong bullish (far right)

    Combines:
    - bias type (directional lean, see BIAS_LEAN)
    - PCR lean (context, same spirit as the signal engine -- puts-heavy leans
      bullish, calls-heavy leans bearish)
    - ADX-based momentum multiplier -- in a TRENDING regime the needle swings
      further toward the extreme (fast/decisive move); in a RANGING/low-ADX
      regime it's pulled back toward center (slow/no real momentum) -- this is
      the "speed" dimension distance-from-center naturally encodes both
      direction AND how strongly/fast it's moving.
    - a further push toward the extreme if the signal is actually tradeable
      (multiple independent checks already agreed).

    Purely a DISPLAY aggregate -- does not feed back into trading decisions.
    """
    # `is not None`, not a truthy check -- PCR==0.0 (no put OI at all) is a
    # real, maximally-bearish reading, not the same as "PCR unavailable".
    pcr_component = max(-25, min(25, (pcr - 1.0) * 25)) if pcr is not None else 0
    bias_component = BIAS_LEAN.get(bias, 0)

    adx = (market_structure or {}).get("adx")
    regime = (market_structure or {}).get("regime")
    if regime == "TRENDING":
        momentum_mult = 1.3
        speed_label, speed_emoji = "High Speed", "⚡"
    elif regime == "RANGING":
        momentum_mult = 0.6
        speed_label, speed_emoji = "Low Speed", "🐢"
    elif regime == "TRANSITIONING":
        momentum_mult = 0.9
        speed_label, speed_emoji = "Building", "🌊"
    else:
        momentum_mult = 1.0
        speed_label, speed_emoji = "Unknown", "❔"

    raw_lean = (pcr_component + bias_component) * momentum_mult

    confidence_bonus = 0
    if signal and signal.get("tradeable"):
        direction_sign = 1 if signal.get("direction") == "CE" else -1
        confidence_bonus = direction_sign * 10

    score = max(0, min(100, round(50 + raw_lean + confidence_bonus)))

    if score >= 85:
        zone = "STRONG BULLISH"
    elif score >= 65:
        zone = "BULLISH"
    elif score >= 55:
        zone = "WEAK BULLISH"
    elif score > 45:
        zone = "NEUTRAL"
    elif score > 35:
        zone = "WEAK BEARISH"
    elif score > 15:
        zone = "BEARISH"
    else:
        zone = "STRONG BEARISH"

    return {
        "score": score, "zone": zone, "speed_label": speed_label, "speed_emoji": speed_emoji,
        "adx": adx, "regime": regime or "UNKNOWN",
        "components": {"pcr_lean": round(pcr_component, 1), "bias_lean": bias_component,
                        "momentum_mult": momentum_mult, "confidence_bonus": confidence_bonus},
    }


def _clamp(value, lo=-100, hi=100):
    return max(lo, min(hi, value))


NEW_TREND_METER_BASE_WEIGHTS = {"money_flow": 0.35, "oi_balance": 0.25, "pcr": 0.15, "price_vwap": 0.15, "greeks": 0.10}
ICHIMOKU_TREND_WEIGHT = 0.20   # base weight when ichimoku is supplied -- other 5 rescale to make room, see below
HIGH_VOLATILITY_ATR_PCT = 0.4   # heuristic threshold (ATR as % of underlying), not yet empirically validated
DISAGREEMENT_MIN_MAGNITUDE = 10   # sub-scores below this are "noise", not a real directional opinion


def compute_new_trend_meter(rows, atm, pcr, pcr_history, underlying, vwap=None, step=50,
                             ichimoku=None, market_structure=None, is_expiry_today=None):
    """
    EXPERIMENTAL Institutional Weighted Decision Engine -- a composite built
    from real, already-available data (OI, Volume, Premium, PCR, Greeks,
    and optionally Ichimoku/regime). NOT machine learning. NOT a prediction
    of future price/timing.

    HONEST LIMITATION: the specific weights below are NOT empirically
    validated against historical outcomes -- they were requested as a
    starting formula, not derived from backtesting. This is why it's shown
    ALONGSIDE the existing compute_trend_meter() (toggle-controlled), not
    replacing it, and why it stays advisory/display-only (see
    ichimoku_engine.py's module docstring) until backtest.py's logged
    recommendation-vs-outcome sample is large enough to judge it.

    BASE sub-scores (each -100..+100), always computed:
      1. Money Flow (35% base): premium x volume, weighted toward whichever
         side (CE/PE) has genuinely fresh (positive) OI-change -- i.e. money
         flowing into NEW positions, not just existing ones changing hands.
      2. OI Balance (25% base): net Put-OI-change minus net Call-OI-change
         across strikes near ATM (put-writing/unwinding vs call/PE writing).
      3. PCR Trend (15% base): is PCR genuinely rising or falling vs its
         recent average (same logic already used in OI-Buildup Conviction
         Strength).
      4. Price vs VWAP (15% base): simple, honest positional check -- above
         VWAP leans bullish, below leans bearish. No pattern-recognition.
      5. Greeks Lean (10% base): net Delta-weighted lean across nearby
         CE/PE strikes, IF Greeks data is available this cycle; contributes
         0 if unavailable rather than guessing.

    OPTIONAL 6th sub-score (only if `ichimoku` -- ichimoku_engine.analyze()'s
    output dict -- is supplied):
      6. Ichimoku Trend (20% base): direction from `ichimoku['entry_signal']`
         (BUY/STRONG BUY=+, SELL/STRONG SELL=-, WAIT/NO_TRADE=0) scaled by
         `ichimoku['confidence_score']`.

    Passing NONE of ichimoku/market_structure/is_expiry_today reproduces
    the ORIGINAL 5-factor 35/25/15/15/10 output exactly -- this extension
    is purely additive, never a breaking change for existing callers.

    DYNAMIC (not fixed) weighting: when the extra context is supplied, base
    weights are nudged per current regime, then renormalized back to sum to
    1.0 -- every multiplier applied is returned in `regime_weighting` (no
    black-box reweighting):
      TRENDING (market_structure['regime']): Ichimoku weight x1.4 (a trend-
        confirmation tool is most informative when there IS a trend).
      RANGING: Ichimoku x0.5, OI Balance/Price-vs-VWAP/PCR x1.3 each (in a
        range, OI positioning/VWAP/PCR read the market better than a
        trend-follower -- see test_ichimoku_scenarios.py's documented
        sideways-market false-signal limitation for WHY this matters).
      High volatility (ATR >= HIGH_VOLATILITY_ATR_PCT% of underlying): OI
        Balance x1.2 (institutional positioning matters more when the tape
        is moving fast).
      Expiry day (`is_expiry_today`): OI Balance x1.5, PCR x1.3, Ichimoku
        x0.5 (option-chain/OI dynamics dominate expiry-day price action).
      Low volume (near-ATM CE+PE volume this cycle is 0): final confidence
        is cut by LOW_VOLUME_CONFIDENCE_MULT regardless of weighting --
        thin volume makes every OTHER module's reading less trustworthy
        too, not just one sub-score.

    Ichimoku is a TREND-CONFIRMATION module, not one more vote averaged in.
    If Ichimoku's own lean OPPOSES the other five modules' combined lean
    (both meaningfully non-neutral, >= DISAGREEMENT_MIN_MAGNITUDE), the
    final `recommendation` is forced to WAIT -- UNLESS the other five show
    strong INTERNAL agreement among themselves (>=2 of them individually
    >=40 in the same direction), in which case Ichimoku is treated as the
    outlier the institutional data overrides rather than silently averaged
    away (`disagreement`/`forced_wait`/`strong_internal_agreement` in the
    return show exactly which branch fired).

    `recommendation` (STRONG BUY/BUY/WAIT/SELL/STRONG SELL) additionally
    requires a MAJORITY of sub-scores to agree with the final direction
    before it can read STRONG -- "multiple institutional modules align",
    never forced from one strong sub-score alone. `zone` (the original
    STRONG BULLISH..STRONG BEARISH text) keeps its EXACT original
    thresholds/wording either way -- existing consumers of `zone` see no
    change.

    Returns a dict with the final score/zone/confidence/recommendation PLUS
    every sub-score and every weight/multiplier applied, so every number is
    traceable back to what produced it (no black-box output).
    """
    nearby = [r for r in rows if abs(r.strike - atm) <= step * 3]

    # 1. Money Flow Score
    call_flow = sum(r.ce_ltp * r.ce_vol for r in nearby if r.ce_oi_chg and r.ce_oi_chg > 0)
    put_flow = sum(r.pe_ltp * r.pe_vol for r in nearby if r.pe_oi_chg and r.pe_oi_chg > 0)
    total_flow = call_flow + put_flow
    money_flow_score = _clamp(round((put_flow - call_flow) / total_flow * 100, 1)) if total_flow > 0 else 0.0

    # 2. OI Balance Score
    net_put_oi_chg = sum(r.pe_oi_chg or 0 for r in nearby)
    net_call_oi_chg = sum(r.ce_oi_chg or 0 for r in nearby)
    total_oi_chg = abs(net_put_oi_chg) + abs(net_call_oi_chg)
    oi_balance_score = _clamp(round((net_put_oi_chg - net_call_oi_chg) / total_oi_chg * 100, 1)) if total_oi_chg > 0 else 0.0

    # 3. PCR Trend Score
    pcr_values = [p for p in (pcr_history or []) if p is not None]
    pcr_score = 0.0
    if len(pcr_values) >= 3 and pcr is not None:
        recent_avg = sum(pcr_values[-3:]) / 3
        if recent_avg > 0:
            pct_change = (pcr - recent_avg) / recent_avg * 100
            pcr_score = _clamp(round(pct_change * 5, 1))   # scaled -- a 20% PCR move maxes the score

    # 4. Price vs VWAP Score
    price_score = 0.0
    if vwap and underlying:
        pct_dist = (underlying - vwap) / vwap * 100
        price_score = _clamp(round(pct_dist * 20, 1))   # scaled -- a 5% move off VWAP maxes the score

    # 5. Greeks Lean Score (only if genuinely available this cycle) -- OI-CHANGE
    # weighted net delta exposure, NOT a raw sum of ce_delta+pe_delta. A plain
    # sum over a symmetric window around ATM self-cancels via put-call parity
    # (ce_delta - |pe_delta| ~= 1 per strike, a function of moneyness DISTANCE
    # from ATM, not of sentiment) and stays near 0 by construction regardless
    # of actual market conditions. Weighting each strike's delta by its OI
    # CHANGE this cycle instead measures where NEW delta exposure is actually
    # building (fresh call- vs put-side positioning) -- same
    # weight-by-this-cycle's-OI-change methodology as the OI Balance Score above.
    greeks_score = 0.0
    delta_rows = [r for r in nearby if (r.ce_delta or r.pe_delta) and (r.ce_oi_chg or r.pe_oi_chg)]
    if delta_rows:
        call_delta_exposure = sum((r.ce_delta or 0) * (r.ce_oi_chg or 0) for r in delta_rows)
        put_delta_exposure = sum((r.pe_delta or 0) * (r.pe_oi_chg or 0) for r in delta_rows)   # pe_delta already signed negative
        net_delta_exposure = call_delta_exposure + put_delta_exposure
        total_abs_exposure = sum(
            abs((r.ce_delta or 0) * (r.ce_oi_chg or 0)) + abs((r.pe_delta or 0) * (r.pe_oi_chg or 0))
            for r in delta_rows
        )
        greeks_score = _clamp(round(net_delta_exposure / total_abs_exposure * 100, 1)) if total_abs_exposure > 0 else 0.0

    sub_scores_named = {
        "money_flow": money_flow_score, "oi_balance": oi_balance_score,
        "pcr": pcr_score, "price_vwap": price_score, "greeks": greeks_score,
    }
    weights = dict(NEW_TREND_METER_BASE_WEIGHTS)

    ichimoku_score = 0.0
    if ichimoku:
        ichimoku_direction = (
            1 if ichimoku.get("entry_signal") in ("BUY", "STRONG BUY") else
            -1 if ichimoku.get("entry_signal") in ("SELL", "STRONG SELL") else 0
        )
        ichimoku_score = _clamp(round(ichimoku_direction * (ichimoku.get("confidence_score") or 0), 1))
        sub_scores_named["ichimoku"] = ichimoku_score
        weights = {k: v * (1 - ICHIMOKU_TREND_WEIGHT) for k, v in weights.items()}
        weights["ichimoku"] = ICHIMOKU_TREND_WEIGHT

    # --- Dynamic (not fixed) regime-aware weight multipliers ---
    regime = (market_structure or {}).get("regime")
    atr = (market_structure or {}).get("atr_14")
    atr_pct = round(atr / underlying * 100, 3) if (atr is not None and underlying) else None
    high_volatility = bool(atr_pct is not None and atr_pct >= HIGH_VOLATILITY_ATR_PCT)
    near_atm_volume = sum((r.ce_vol or 0) + (r.pe_vol or 0) for r in nearby)
    low_volume = near_atm_volume == 0

    applied_multipliers = {}
    if "ichimoku" in weights and regime == "TRENDING":
        applied_multipliers["ichimoku"] = applied_multipliers.get("ichimoku", 1.0) * 1.4
    elif "ichimoku" in weights and regime == "RANGING":
        applied_multipliers["ichimoku"] = applied_multipliers.get("ichimoku", 1.0) * 0.5
        for k in ("oi_balance", "price_vwap", "pcr"):
            applied_multipliers[k] = applied_multipliers.get(k, 1.0) * 1.3
    if high_volatility:
        applied_multipliers["oi_balance"] = applied_multipliers.get("oi_balance", 1.0) * 1.2
    if is_expiry_today:
        applied_multipliers["oi_balance"] = applied_multipliers.get("oi_balance", 1.0) * 1.5
        applied_multipliers["pcr"] = applied_multipliers.get("pcr", 1.0) * 1.3
        if "ichimoku" in weights:
            applied_multipliers["ichimoku"] = applied_multipliers.get("ichimoku", 1.0) * 0.5

    for k, mult in applied_multipliers.items():
        weights[k] *= mult
    weight_total = sum(weights.values()) or 1.0
    weights = {k: v / weight_total for k, v in weights.items()}   # renormalize back to sum 1.0

    final_score = _clamp(round(sum(sub_scores_named[k] * weights[k] for k in weights)))

    if final_score >= 80:
        zone = "STRONG BULLISH"
    elif final_score >= 50:
        zone = "BULLISH"
    elif final_score >= 20:
        zone = "MILD BULLISH"
    elif final_score > -20:
        zone = "SIDEWAYS"
    elif final_score > -50:
        zone = "MILD BEARISH"
    elif final_score > -80:
        zone = "BEARISH"
    else:
        zone = "STRONG BEARISH"

    # Confidence: how many sub-scores AGREE with the final direction (same
    # "conviction strength" principle used elsewhere) -- reduces confidence
    # honestly when signals conflict, rather than guessing. Identical
    # formula/divisor (5) to the original when ichimoku is absent.
    if final_score > 0:
        agreeing_count = sum(1 for s in sub_scores_named.values() if s > 0)
    elif final_score < 0:
        agreeing_count = sum(1 for s in sub_scores_named.values() if s < 0)
    else:
        agreeing_count = 0
    confidence = round(agreeing_count / len(sub_scores_named) * 100)

    # --- Ichimoku as TREND-CONFIRMATION, not an extra vote: disagreement
    # cuts confidence and can force WAIT; see docstring for the exact rule. ---
    disagreement, forced_wait, strong_internal_agreement = False, False, 0
    institutional_score = final_score
    if "ichimoku" in weights:
        institutional_weight_total = sum(v for k, v in weights.items() if k != "ichimoku") or 1e-9
        institutional_score = _clamp(round(sum(
            sub_scores_named[k] * weights[k] for k in weights if k != "ichimoku"
        ) / institutional_weight_total))
        if abs(institutional_score) >= DISAGREEMENT_MIN_MAGNITUDE and abs(ichimoku_score) >= DISAGREEMENT_MIN_MAGNITUDE:
            disagreement = (institutional_score > 0) != (ichimoku_score > 0)
            if disagreement:
                strong_internal_agreement = sum(
                    1 for k, v in sub_scores_named.items()
                    if k != "ichimoku" and ((institutional_score > 0 and v >= 40) or (institutional_score < 0 and v <= -40))
                )
                if strong_internal_agreement < 2:
                    forced_wait = True
                    confidence = round(confidence * 0.5)   # honestly reduced, not silently kept high

    if low_volume:
        confidence = round(confidence * 0.6)   # thin volume -> every module's reading is less trustworthy right now

    majority_threshold = len(sub_scores_named) // 2 + 1
    if forced_wait:
        recommendation = "WAIT"
    elif final_score > 20:
        recommendation = "STRONG BUY" if (final_score >= 70 and agreeing_count >= majority_threshold) else "BUY"
    elif final_score < -20:
        recommendation = "STRONG SELL" if (final_score <= -70 and agreeing_count >= majority_threshold) else "SELL"
    else:
        recommendation = "WAIT"

    if final_score >= 80:
        trend_strength = "Very Strong"
    elif final_score >= 50:
        trend_strength = "Strong"
    elif final_score >= 20:
        trend_strength = "Moderate"
    else:
        trend_strength = "Weak"

    normalized = (final_score + 100) / 2
    trade_quality = (
        "Low" if forced_wait or low_volume else
        "High" if agreeing_count >= majority_threshold and not disagreement else
        "Medium" if agreeing_count >= 2 else "Low"
    )
    entry_quality, cloud_strength, cloud_direction, dynamic_support, dynamic_resistance, trend_stage, momentum = (
        None, None, None, None, None, None, None)
    if ichimoku:
        momentum = ichimoku.get("momentum")
        cloud_direction = (ichimoku.get("cloud") or {}).get("direction")
        dynamic_support = ichimoku.get("support")
        dynamic_resistance = ichimoku.get("resistance")
        trend_stage = ichimoku.get("trend_stage")
        thickness = (ichimoku.get("cloud") or {}).get("thickness")
        if thickness is not None and atr:
            cloud_strength = min(100, round(thickness / atr * 100))
        detail = ichimoku.get("signal_detail") or {}
        if detail.get("conditions_total"):
            pct = detail["conditions_passed"] / detail["conditions_total"] * 100
            entry_quality = "High" if pct >= 70 else "Medium" if pct >= 50 else "Low"

    reasons = []
    for name, sc in sub_scores_named.items():
        if abs(sc) >= 15:
            reasons.append(f"{name.replace('_', ' ')} {'bullish' if sc > 0 else 'bearish'} ({sc:+.0f})")
    if ichimoku and ichimoku.get("reasons"):
        reasons.extend(f"ichimoku: {r}" for r in ichimoku["reasons"][:5])
    if disagreement:
        reasons.append(
            f"CONFLICT: institutional modules read {('bullish' if institutional_score > 0 else 'bearish')} "
            f"but Ichimoku reads {('bullish' if ichimoku_score > 0 else 'bearish')}"
            + (" -- overridden by strong institutional agreement" if not forced_wait else " -- forced to WAIT")
        )
    if low_volume:
        reasons.append("near-ATM volume is 0 this cycle -- confidence reduced")

    return {
        "score": final_score, "zone": zone, "confidence": confidence,
        "recommendation": recommendation,
        "money_flow_pct": money_flow_score, "oi_balance_pct": oi_balance_score,
        "pcr_pct": pcr_score, "price_pct": price_score, "greeks_pct": greeks_score,
        "ichimoku_pct": ichimoku_score if ichimoku else None,
        "greeks_available": bool(delta_rows),
        "weights": {k: round(v, 3) for k, v in weights.items()},
        "regime_weighting": {"regime": regime, "high_volatility": high_volatility, "atr_pct": atr_pct,
                              "is_expiry_today": bool(is_expiry_today), "low_volume": low_volume,
                              "multipliers_applied": {k: round(v, 2) for k, v in applied_multipliers.items()}},
        "institutional_score": institutional_score if ichimoku else None,
        "disagreement": disagreement, "forced_wait": forced_wait,
        "strong_internal_agreement": strong_internal_agreement,
        "bullish_pct": round(normalized, 1), "bearish_pct": round(100 - normalized, 1),
        "trend_strength": trend_strength, "institutional_bias": "Conflicted" if disagreement else zone.split()[-1].title() if final_score != 0 else "Neutral",
        "trade_quality": trade_quality, "entry_quality": entry_quality,
        "momentum": momentum, "cloud_strength": cloud_strength, "cloud_direction": cloud_direction,
        "dynamic_support": dynamic_support, "dynamic_resistance": dynamic_resistance,
        "trend_stage": trend_stage,
        "risk_level": "High" if high_volatility else "Elevated" if regime == "TRANSITIONING" else "Normal",
        "reasons": reasons,
        "note": "EXPERIMENTAL -- weights not yet empirically validated. Advisory/display-only: does not place or gate real orders. "
                "Shown for comparison alongside the existing trend-meter, not as a replacement.",
    }


def generate_signal(rows, atm, bias, note, pcr, support, resistance,
                     target_delta_approx=0.55, sl_percent=0.35, min_target_percent=0.15,
                     confidence_threshold=60, nse_atm_row=None, underlying=None, expiry_date=None,
                     strike_step=50, source_label="NSE", market_structure=None,
                     structural_proximity_atr_mult=0.5, structural_bonus=20, structural_penalty=30):
    """
    Rule-based signal: CE/PE direction, strike, entry, target, SL, confidence.
    NOT ML, NOT guaranteed -- your OI logic made systematic.

    nse_atm_row: the ATM StrikeRow from NSE's independent feed, if available --
                 supplies IV (for real Black-Scholes delta) and order-flow
                 (buy/sell quantity) data Angel One doesn't reliably give.
                 None if NSE data wasn't available this cycle (Angel-only signal,
                 falls back to a flat delta approximation).
    underlying, expiry_date: needed alongside nse_atm_row to compute real delta
                 (spot price and a date object for the option's expiry).
    market_structure: output of market_structure.build_market_structure(), if
                 available -- used as a QUALITY GATE. Breakouts/reversals near a
                 genuine structural level (PDH/PDL, swing high/low, opening range,
                 pivots) get a confidence boost; setups nowhere near any real
                 level get penalized. This is what turns "a signal fires on any
                 nearby OI wall, constantly" into "only take the setups that
                 matter" -- fewer, higher-quality trades instead of trading every
                 minor OI wobble.
    """
    atm_row = next((r for r in rows if r.strike == atm), None)
    if not atm_row:
        return {"action": "NO_TRADE", "reason": "ATM strike data unavailable"}

    if "BULLISH" in bias or bias == "REVERSAL UP RISK":
        direction = "CE"
    elif "BEARISH" in bias or bias == "REVERSAL DOWN RISK":
        direction = "PE"
    else:
        return {"action": "NO_TRADE", "reason": "Bias is NEUTRAL/RANGE -- no directional edge right now."}

    entry_price = atm_row.ce_ltp if direction == "CE" else atm_row.pe_ltp
    if not entry_price or entry_price <= 0:
        return {"action": "NO_TRADE", "reason": f"No valid LTP for {atm} {direction}."}

    delta_used = target_delta_approx
    delta_source = f"flat approximation (no {source_label} IV available this cycle)"
    if nse_atm_row is not None and underlying and expiry_date:
        iv = nse_atm_row.ce_iv if direction == "CE" else nse_atm_row.pe_iv
        if iv and iv > 0:
            T = time_to_expiry_years(expiry_date)
            g = black_scholes_greeks(underlying, atm, T, iv, direction)
            if g.get("valid"):
                delta_used = abs(g["delta"])
                delta_source = f"{source_label} IV={iv}% -> Black-Scholes delta"

    confidence = 50
    if pcr > 1.3 or pcr < 0.7:
        confidence += 15
    signal_field = atm_row.ce_signal if direction == "CE" else atm_row.pe_signal
    if signal_field in ("Short Covering", "Short Buildup"):
        confidence += 15
    this_vol = atm_row.ce_vol if direction == "CE" else atm_row.pe_vol
    other_vol = atm_row.pe_vol if direction == "CE" else atm_row.ce_vol
    if other_vol and this_vol > other_vol * 1.2:
        confidence += 10
    if "BREAKOUT" in bias or "BREAKDOWN" in bias:
        confidence += 10

    structural_note = ""
    if market_structure and underlying:
        level = nearest_major_level(underlying, market_structure)
        atr = market_structure.get("atr_14")
        # `atr is not None` rather than a truthy check -- ATR can legitimately
        # be exactly 0.0 (flat/no-range recent candles), which is a real
        # reading, not the same as "not computed yet" (None).
        if level and atr is not None and atr > 0:
            max_dist = atr * structural_proximity_atr_mult
            if level["distance"] <= max_dist:
                confidence += structural_bonus
                structural_note = f"Near {level['name']} ({level['price']}, {level['distance']} pts away) -- high-quality setup at a real structural level."
            else:
                confidence -= structural_penalty
                structural_note = f"Not near any major level (nearest: {level['name']} is {level['distance']} pts away, ATR={atr}) -- low conviction, skip unless everything else is very strong."
        elif atr is None:
            structural_note = "Market structure still building (not enough candle history yet) -- quality gate not active."
        elif atr == 0:
            structural_note = "ATR is 0 (no measurable volatility in recent candles) -- structural quality gate skipped."

    regime_note = ""
    if market_structure and market_structure.get("regime") not in (None, "UNKNOWN"):
        regime = market_structure["regime"]
        is_trend_continuation = "BREAKOUT" in bias or "BREAKDOWN" in bias
        is_reversal_fade = "REVERSAL" in bias
        if regime == "TRENDING":
            if is_trend_continuation:
                confidence += 15
                regime_note = f"Market is TRENDING (ADX={market_structure.get('adx')}) -- breakout-continuation setups favored right now."
            elif is_reversal_fade:
                confidence -= 20
                regime_note = f"Market is TRENDING (ADX={market_structure.get('adx')}) -- counter-trend reversal/fade setups are risky here, penalized."
        elif regime == "RANGING":
            if is_reversal_fade:
                confidence += 15
                regime_note = f"Market is RANGING (ADX={market_structure.get('adx')}) -- mean-reversion/fade setups favored right now."
            elif is_trend_continuation:
                confidence -= 15
                regime_note = f"Market is RANGING (ADX={market_structure.get('adx')}) -- breakouts often fail (fakeout risk) in range-bound conditions, penalized."

    dual_source_note = ""
    order_flow_note = ""
    if nse_atm_row is not None:
        # -- dual-source agreement: does NSE's OWN ATM signal point the same way? --
        nse_signal_field = nse_atm_row.ce_signal if direction == "CE" else nse_atm_row.pe_signal
        bullish_flavored = ("Short Covering", "Long Buildup")
        bearish_flavored = ("Short Buildup", "Long Unwinding")
        angel_bullish = signal_field in bullish_flavored
        nse_bullish = nse_signal_field in bullish_flavored
        angel_bearish = signal_field in bearish_flavored
        nse_bearish = nse_signal_field in bearish_flavored
        if (angel_bullish and nse_bullish) or (angel_bearish and nse_bearish):
            confidence += 15
            dual_source_note = "Angel One + NSE agree on this strike's OI read."
        elif (angel_bullish and nse_bearish) or (angel_bearish and nse_bullish):
            confidence -= 15
            dual_source_note = "CAUTION: Angel One and NSE disagree on this strike's OI read (single-source signal)."

        # -- order-flow imbalance (NSE totalBuyQuantity vs totalSellQuantity) --
        buy_qty = nse_atm_row.ce_buy_qty if direction == "CE" else nse_atm_row.pe_buy_qty
        sell_qty = nse_atm_row.ce_sell_qty if direction == "CE" else nse_atm_row.pe_sell_qty
        if buy_qty and sell_qty:
            if buy_qty > sell_qty * 1.3:
                confidence += 10
                order_flow_note = "Order book: buyers dominate this strike."
            elif sell_qty > buy_qty * 1.3:
                confidence -= 10
                order_flow_note = "Order book: sellers dominate this strike -- against the trade direction."

    confidence = max(20, min(95, confidence))

    walls = resistance if direction == "CE" else support
    wall_strike = walls[0].strike if walls else atm

    cross_verify_note = ""
    wall_confirmed, matched_level = cross_verify_wall(wall_strike, direction, market_structure or {})
    if wall_confirmed:
        confidence += 15
        cross_verify_note = (
            f"OI-wall at {wall_strike} is CONFIRMED by {matched_level['name']} "
            f"({matched_level['price']}, {matched_level['distance']} pts apart) -- "
            f"two independent S/R systems agree, extra-strong level."
        )
    confidence = max(20, min(95, confidence))

    underlying_move = abs(wall_strike - atm)
    projected_move = underlying_move * delta_used
    target_price = round(entry_price + max(projected_move, entry_price * min_target_percent), 2)

    # -- SL: OI-wall-based invalidation instead of a flat percentage. The idea:
    #    if the underlying moves back past the nearest OPPOSING wall, the OI
    #    thesis behind this trade has failed -- that's the natural stop point,
    #    projected onto the option premium via the same delta used for target. --
    # `support`/`resistance` are the top-3 walls by OI (see oi_walls()), NOT
    # sorted by distance -- iterating them in that order and taking the first
    # match picked whichever had the MOST OI, which can be several strikes
    # further away than the true nearest one. Pick by minimum distance
    # instead, among the candidates on the correct (opposing) side of ATM.
    opposing_walls = support if direction == "CE" else resistance
    candidates = [
        w for w in opposing_walls
        if (direction == "CE" and w.strike < atm) or (direction == "PE" and w.strike > atm)
    ]
    invalidation_strike = min(candidates, key=lambda w: abs(w.strike - atm)).strike if candidates else None
    if invalidation_strike is None:
        invalidation_strike = atm - strike_step if direction == "CE" else atm + strike_step

    sl_underlying_distance = abs(atm - invalidation_strike)
    sl_projected_move = sl_underlying_distance * delta_used
    sl_price_structural = round(entry_price - sl_projected_move, 2)
    sl_price_floor = round(entry_price * (1 - sl_percent), 2)   # safety net: never risk more than sl_percent
    sl_price = max(sl_price_structural, sl_price_floor, 0.05)   # tighter of the two, never below 5 paise

    reason_parts = [note]
    if structural_note:
        reason_parts.append(structural_note)
    if cross_verify_note:
        reason_parts.append(cross_verify_note)
    if regime_note:
        reason_parts.append(regime_note)
    if dual_source_note:
        reason_parts.append(dual_source_note)
    if order_flow_note:
        reason_parts.append(order_flow_note)

    return {
        "action": f"BUY {direction}",
        "strike": atm,
        "direction": direction,
        "entry_price": entry_price,
        "target_price": target_price,
        "sl_price": sl_price,
        "target_points": round(target_price - entry_price, 2),
        "sl_points": round(entry_price - sl_price, 2),
        "confidence": confidence,
        "tradeable": confidence >= confidence_threshold,
        "reason": "  |  ".join(reason_parts),
        "delta_used": round(delta_used, 3),
        "delta_source": delta_source,
        "sl_invalidation_strike": invalidation_strike,
        "dual_source_checked": nse_atm_row is not None,
        "wall_cross_verified": wall_confirmed,
    }
