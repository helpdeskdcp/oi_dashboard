"""
agents/trading_intelligence/strike_intelligence.py -- Module 2b, per the
explicit follow-up request: "Milestone 10 madhye tumhi vaparat asलेल्या
Option Chain screen sarkhi AI Intelligence tayar kara -- BATI pratyek
strike sathi swataha OI Wall / Support-Resistance / PCR / Build-up Type /
Max Pain / Expected Move / AI Buy-Sell Probability / CE-PE Strength
dakhavel." A per-strike table, the same shape as the live Option Chain
screen this repo already has (app.py's own strikes view) -- every field
below is either read straight off the already-live StrikeRow, or a small,
honestly-scoped derived score, never a duplicate OI/PCR/Greeks computation
(those stay owned by oi_engine.py, greeks.py -- see market_data.py and
institutional_intelligence.py, which this module reuses rather than
re-querying the database itself).

Expected Move: the standard, textbook options formula (spot * IV * sqrt(T),
one standard deviation by expiry) -- NOT proprietary, NOT AI-predicted.

AI Buy/Sell Probability: an HONEST label for what this actually is --
oi_engine.net_oi_buildup_lean's own -2..+2 lean, linearly mapped to a
10-90% band (never claiming 0% or 100% certainty, the same clamping
philosophy oi_engine.generate_signal's own confidence score already uses).
This is a POSITIONING read (what the OI data says right now), not a
back-tested win-rate probability -- see ai_trading_engine.py's own
`probability` field for the genuinely calibrated, historical-trade-based
number, which is a DIFFERENT thing from this per-strike lean and must
never be confused with it.

CE/PE Strength: a transparent, documented composite of (a) this strike's
share of total CE/PE OI across the chain, and (b) whether its signal type
indicates fresh conviction (Long/Short Buildup) vs. fading conviction
(Short Covering/Long Unwinding) -- not a black box.
"""
import dataclasses
import datetime as dt

from oi_engine import calc_max_pain, net_oi_buildup_lean, oi_walls


@dataclasses.dataclass
class StrikeIntelligence:
    strike: int
    is_oi_wall_support: bool
    is_oi_wall_resistance: bool
    is_max_pain: bool
    ce_buildup_type: str
    pe_buildup_type: str
    net_lean: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    ai_buy_probability_pct: int  # CE-side lean, 10-90
    ai_sell_probability_pct: int  # PE-side lean, 10-90
    ce_strength: int  # 0-100
    pe_strength: int  # 0-100
    expected_move_pts: float | None
    ce_oi: int
    pe_oi: int
    ce_ltp: float
    pe_ltp: float


def _expected_move(underlying: float | None, iv_percent: float | None, expiry_date: dt.date | None) -> float | None:
    """Standard 1-SD expected move by expiry: spot * IV * sqrt(T). None if
    any input is missing or IV is non-positive -- never a fabricated
    number from a degenerate input."""
    if not underlying or not iv_percent or iv_percent <= 0 or not expiry_date:
        return None
    from greeks import time_to_expiry_years
    T = time_to_expiry_years(expiry_date)
    import math
    return round(underlying * (iv_percent / 100) * math.sqrt(T), 2)


def _strength(oi: int, total_oi: int, signal: str) -> int:
    """0-100: OI-share-of-chain (up to 70 points, capped) scaled by a
    conviction multiplier from the signal type (fresh positioning counts
    more than fading positioning) -- transparent arithmetic, not a model."""
    if total_oi <= 0:
        return 0
    share_pts = min(70, (oi / total_oi) * 100 * 2)  # a strike with 35%+ of chain OI already maxes this term
    conviction_mult = {"Long Buildup": 1.0, "Short Buildup": 1.0, "Short Covering": 0.6,
                        "Long Unwinding": 0.4, "Neutral": 0.5}.get(signal, 0.5)
    return round(min(100, share_pts * (1 + conviction_mult) / 1.7))


def _probability_from_lean(lean: int) -> int:
    """-1/0/1 -> 35/50/65-ish band; -2..2 (net_lean scale, used for the
    combined "net_lean" field elsewhere) also passes through cleanly.
    Clamped to [10, 90] -- never 0% or 100%, matching
    oi_engine.generate_signal's own confidence-clamping philosophy."""
    return max(10, min(90, 50 + lean * 20))


def build_table(rows: list, *, underlying: float | None = None, expiry_date: dt.date | None = None) -> list:
    """One StrikeIntelligence per strike in `rows` -- the full per-strike
    table the Option Chain-style view displays."""
    if not rows:
        return []
    support, resistance = oi_walls(rows)
    support_strikes = {r.strike for r in support}
    resistance_strikes = {r.strike for r in resistance}
    max_pain = calc_max_pain(rows)
    total_ce_oi = sum(r.ce_oi for r in rows) or 1
    total_pe_oi = sum(r.pe_oi for r in rows) or 1

    table = []
    for r in rows:
        lean = net_oi_buildup_lean(r.ce_signal, r.pe_signal)
        ce_iv_for_move = r.ce_iv if r.ce_iv else r.pe_iv
        table.append(StrikeIntelligence(
            strike=r.strike,
            is_oi_wall_support=r.strike in support_strikes,
            is_oi_wall_resistance=r.strike in resistance_strikes,
            is_max_pain=(r.strike == max_pain),
            ce_buildup_type=r.ce_signal, pe_buildup_type=r.pe_signal,
            net_lean=lean["overall"],
            ai_buy_probability_pct=_probability_from_lean(lean["ce_lean"]),
            ai_sell_probability_pct=_probability_from_lean(-lean["pe_lean"]),
            ce_strength=_strength(r.ce_oi, total_ce_oi, r.ce_signal),
            pe_strength=_strength(r.pe_oi, total_pe_oi, r.pe_signal),
            expected_move_pts=_expected_move(underlying, ce_iv_for_move, expiry_date),
            ce_oi=r.ce_oi, pe_oi=r.pe_oi, ce_ltp=r.ce_ltp, pe_ltp=r.pe_ltp,
        ))
    return table
