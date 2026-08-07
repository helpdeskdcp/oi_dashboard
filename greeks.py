#!/usr/bin/env python3
"""
greeks.py -- Black-Scholes delta/gamma using NSE's implied volatility.

Why this matters for target/SL precision:
Our earlier target/SL math used a flat delta approximation (0.55) for every
ATM option, every symbol, every day. Real delta depends on spot, strike, time
to expiry, and IV -- all of which change constantly. NSE gives us IV per
strike; Angel One doesn't reliably expose it. So we use NSE's IV (when
available) to compute the REAL delta/gamma for the specific option being
traded, and fall back to the flat approximation only when IV data is missing
(e.g., NSE unavailable that cycle).

Standard Black-Scholes formulas -- no proprietary math, textbook quant.
"""

import math
import datetime as dt


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def time_to_expiry_years(expiry_date: dt.date, now: dt.datetime = None) -> float:
    """Years remaining until 15:30 IST on expiry_date. Floored at ~1 minute to
    avoid division by zero on/after expiry."""
    now = now or dt.datetime.now()
    expiry_dt = dt.datetime.combine(expiry_date, dt.time(15, 30))
    seconds_left = (expiry_dt - now).total_seconds()
    seconds_left = max(seconds_left, 60)
    return seconds_left / (365 * 24 * 3600)


def black_scholes_greeks(spot: float, strike: float, time_years: float, iv_percent: float,
                          option_type: str, risk_free_rate: float = 0.065) -> dict:
    """
    Returns {"delta": ..., "gamma": ..., "prob_itm": ...}. delta is signed
    (negative for puts). prob_itm is the risk-neutral probability of
    finishing in-the-money (N(d2) for calls, N(-d2) for puts) -- added for
    Milestone 10's Strike Intelligence "Probability of ITM" field, additive
    and backward compatible (existing callers reading only "delta"/"gamma"/
    "valid" are unaffected). Deliberately NOT the same number as delta,
    despite the common trader shorthand "delta approximates probability
    ITM" -- that's a useful mental approximation, not the same formula
    (delta is N(d1), the real risk-neutral ITM probability is N(d2));
    since this milestone asked for the real thing, it computes the real
    thing. Falls back to a sane flat estimate if inputs are degenerate
    (IV=0, T<=0 etc) rather than raising -- callers shouldn't have to
    special-case bad data. prob_itm is None (never a guessed number) in
    every degenerate/fallback case, consistent with the "valid": False
    flag already meaning "don't trust the other numbers either."
    """
    if spot is None or strike is None or time_years is None or iv_percent is None:
        spot = spot or 0
        strike = strike or 0
        time_years = time_years or 0
        iv_percent = iv_percent or 0
    if spot <= 0 or strike <= 0 or time_years <= 0 or iv_percent <= 0:
        flat = 0.5 if option_type == "CE" else -0.5
        return {"delta": flat, "gamma": 0.0, "prob_itm": None, "valid": False}

    sigma = iv_percent / 100
    T = time_years
    try:
        d1 = (math.log(spot / strike) + (risk_free_rate + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        flat = 0.5 if option_type == "CE" else -0.5
        return {"delta": flat, "gamma": 0.0, "prob_itm": None, "valid": False}

    d2 = d1 - sigma * math.sqrt(T)
    delta = _norm_cdf(d1) if option_type == "CE" else _norm_cdf(d1) - 1
    gamma = _norm_pdf(d1) / (spot * sigma * math.sqrt(T))
    prob_itm = _norm_cdf(d2) if option_type == "CE" else _norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "prob_itm": round(prob_itm, 4), "valid": True}


if __name__ == "__main__":
    # Sanity check: ATM option delta should land close to 0.5 (CE) / -0.5 (PE)
    spot = 24206.9
    strike = 24200
    expiry = dt.date(2026, 7, 14)
    now = dt.datetime(2026, 7, 11, 14, 0)
    T = time_to_expiry_years(expiry, now)
    print(f"Time to expiry: {T*365:.2f} days ({T:.5f} years)")

    for iv in (15, 20, 30):
        ce = black_scholes_greeks(spot, strike, T, iv, "CE")
        pe = black_scholes_greeks(spot, strike, T, iv, "PE")
        print(f"IV={iv}%  CE delta={ce['delta']:.3f} gamma={ce['gamma']:.6f}  |  PE delta={pe['delta']:.3f} gamma={pe['gamma']:.6f}")

    # OTM/ITM sanity: higher strike CE should have LOWER delta than ATM
    otm_ce = black_scholes_greeks(spot, 24400, T, 20, "CE")
    itm_ce = black_scholes_greeks(spot, 24000, T, 20, "CE")
    print(f"\nOTM (24400) CE delta={otm_ce['delta']:.3f}  (should be < ATM delta)")
    print(f"ITM (24000) CE delta={itm_ce['delta']:.3f}  (should be > ATM delta)")
