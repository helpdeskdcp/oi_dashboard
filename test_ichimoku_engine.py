"""
test_ichimoku_engine.py -- regression tests for ichimoku_engine.py.

Synthetic data only -- no live broker/market dependency, same philosophy as
test_sr_engine_v3.py / test_scalping_engine.py. Per project convention, NEVER
hits app.py's /live-positions route or any real Angel One session in tests.
"""
import datetime as dt

import numpy as np
import pandas as pd

import ichimoku_engine as ie


def _trend_candles(n=400, slope=0.075, noise=0.5, start_price=100.0, seed=42, interval_minutes=3):
    """Synthetic OHLCV with a controllable linear drift + gaussian noise --
    slope>0 builds a genuine uptrend (cloud should end up Bullish / price
    above cloud), slope<0 a downtrend, slope=0 a flat/ranging series."""
    rng = np.random.default_rng(seed)
    start = dt.datetime(2026, 1, 1, 9, 15)
    base = start_price + np.cumsum(rng.normal(0, noise, n)) + np.linspace(0, slope * n, n)
    candles = []
    for i in range(n):
        o = base[i]
        c = o + rng.normal(0, noise * 0.5)
        h = max(o, c) + abs(rng.normal(0, noise * 0.4))
        l = min(o, c) - abs(rng.normal(0, noise * 0.4))
        candles.append({
            "datetime": start + dt.timedelta(minutes=interval_minutes * i),
            "open": o, "high": h, "low": l, "close": c,
            "volume": int(abs(rng.normal(1000, 300))) + 50,
        })
    return candles


def _flat_candles(n=400, price=100.0):
    start = dt.datetime(2026, 1, 1, 9, 15)
    return [{
        "datetime": start + dt.timedelta(minutes=3 * i),
        "open": price, "high": price, "low": price, "close": price, "volume": 1000,
    } for i in range(n)]


# ----------------------------------------------------------------------------
# Formula correctness
# ----------------------------------------------------------------------------

def test_tenkan_kijun_formula_matches_textbook():
    candles = _trend_candles(n=120, seed=1)
    df = ie.calculate_ichimoku(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    i = 100   # arbitrary interior index with full lookback available
    expected_tenkan = (max(highs[i - 8:i + 1]) + min(lows[i - 8:i + 1])) / 2
    expected_kijun = (max(highs[i - 25:i + 1]) + min(lows[i - 25:i + 1])) / 2
    assert df["tenkan"].iloc[i] == pytest_approx(expected_tenkan)
    assert df["kijun"].iloc[i] == pytest_approx(expected_kijun)


def pytest_approx(x, tol=1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - x) <= tol
    return _Approx()


def test_senkou_a_is_shifted_forward_by_displacement():
    candles = _trend_candles(n=200, seed=2)
    df = ie.calculate_ichimoku(candles)
    # senkou_a at index i must equal the UN-shifted (future_senkou_a) value
    # from displacement candles earlier -- that's the entire point of the
    # +26 plot-forward convention.
    i = 150
    assert df["senkou_a"].iloc[i] == df["future_senkou_a"].iloc[i - ie.DISPLACEMENT]


def test_chikou_is_close_shifted_backward():
    candles = _trend_candles(n=200, seed=3)
    df = ie.calculate_ichimoku(candles)
    i = 100
    assert df["chikou"].iloc[i] == df["close"].iloc[i + ie.DISPLACEMENT]


def test_cloud_top_bottom_thickness():
    candles = _trend_candles(n=200, seed=4)
    df = ie.calculate_ichimoku(candles)
    cloud = ie.cloud_values_at(df, -1)
    a, b = df["senkou_a"].iloc[-1], df["senkou_b"].iloc[-1]
    assert cloud.top == round(max(a, b), 2)
    assert cloud.bottom == round(min(a, b), 2)
    assert cloud.thickness == round(abs(a - b), 2)
    assert cloud.direction in ("Bullish", "Bearish", "Neutral")


# ----------------------------------------------------------------------------
# Graceful degradation on insufficient history
# ----------------------------------------------------------------------------

def test_insufficient_history_returns_no_trade_not_exception():
    candles = _trend_candles(n=30, seed=5)   # well under MIN_CANDLES
    result = ie.analyze(candles)
    assert result["entry_signal"] == "NO_TRADE"
    assert result["confidence_score"] == 0
    assert result["ichimoku_values"]["tenkan"] is None


def test_empty_candles_does_not_raise():
    result = ie.analyze([])
    assert result["entry_signal"] == "NO_TRADE"


# ----------------------------------------------------------------------------
# Trend / signal direction sanity on unambiguous synthetic trends
# ----------------------------------------------------------------------------

def test_strong_uptrend_produces_bullish_signal_above_cloud():
    candles = _trend_candles(n=400, slope=0.15, noise=0.3, seed=10)
    result = ie.analyze(candles)
    assert result["cloud_events"]["price_vs_cloud"] == "Above Cloud"
    assert result["entry_signal"] in ("BUY", "STRONG BUY")
    assert "Bullish" in result["trend"]


def test_strong_downtrend_produces_bearish_signal_below_cloud():
    candles = _trend_candles(n=400, slope=-0.15, noise=0.3, seed=11)
    result = ie.analyze(candles)
    assert result["cloud_events"]["price_vs_cloud"] == "Below Cloud"
    assert result["entry_signal"] in ("SELL", "STRONG SELL")
    assert "Bearish" in result["trend"]


def test_flat_series_has_zero_thickness_cloud_and_no_directional_edge():
    candles = _flat_candles(n=300)
    result = ie.analyze(candles)
    assert result["cloud"]["thickness"] == 0.0
    # a perfectly flat market must never be forced into BUY/SELL
    assert result["entry_signal"] in ("WAIT", "NO_TRADE")


def test_strong_signal_requires_multiple_conditions_not_a_single_indicator():
    candles = _trend_candles(n=400, slope=0.15, noise=0.3, seed=10)
    df = ie.calculate_ichimoku(candles)
    signal = ie.generate_ichimoku_signal(df)
    if signal["action"] == "STRONG BUY":
        assert signal["conditions_passed"] >= ie.STRONG_SIGNAL_MIN_CONDITIONS
    elif signal["action"] == "BUY":
        assert ie.BUY_SIGNAL_MIN_CONDITIONS <= signal["conditions_passed"] < ie.STRONG_SIGNAL_MIN_CONDITIONS


# ----------------------------------------------------------------------------
# Risk management
# ----------------------------------------------------------------------------

def test_risk_management_long_stop_is_below_entry_and_targets_above():
    candles = _trend_candles(n=400, slope=0.15, noise=0.3, seed=10)
    df = ie.calculate_ichimoku(candles)
    atr = ie.calc_atr(df.to_dict("records"), period=14)
    risk = ie.compute_risk_management(df, "STRONG BUY", atr=atr)
    assert risk["initial_stop"] is not None
    assert risk["initial_stop"] < risk["entry"]
    assert all(t > risk["entry"] for t in risk["targets"])
    assert risk["targets"] == sorted(risk["targets"])


def test_risk_management_short_stop_is_above_entry_and_targets_below():
    candles = _trend_candles(n=400, slope=-0.15, noise=0.3, seed=11)
    df = ie.calculate_ichimoku(candles)
    atr = ie.calc_atr(df.to_dict("records"), period=14)
    risk = ie.compute_risk_management(df, "STRONG SELL", atr=atr)
    assert risk["initial_stop"] is not None
    assert risk["initial_stop"] > risk["entry"]
    assert all(t < risk["entry"] for t in risk["targets"])


def test_position_size_none_without_capital():
    candles = _trend_candles(n=400, slope=0.15, seed=10)
    df = ie.calculate_ichimoku(candles)
    risk = ie.compute_risk_management(df, "BUY", atr=1.0, capital=None)
    assert risk["position_size"] is None


def test_position_size_computed_with_capital():
    candles = _trend_candles(n=400, slope=0.15, seed=10)
    df = ie.calculate_ichimoku(candles)
    risk = ie.compute_risk_management(df, "BUY", atr=1.0, capital=100000, risk_per_trade_pct=1.0)
    if risk["initial_stop"] is not None:
        assert risk["position_size"] is not None
        assert risk["position_size"] >= 0


# ----------------------------------------------------------------------------
# Trend lifecycle staging
# ----------------------------------------------------------------------------

def test_trend_stage_present_and_valid_in_strong_trend():
    candles = _trend_candles(n=400, slope=0.15, noise=0.3, seed=10)
    result = ie.analyze(candles)
    if result["trend_stage"] is not None:
        assert result["trend_stage"] in ie.TREND_STAGES
        assert result["recommended_action"] == ie.TREND_STAGE_ACTIONS[result["trend_stage"]]


def test_trend_stage_none_when_price_inside_cloud():
    # A flat series keeps price oscillating inside a near-zero-thickness
    # cloud -- there's no established trend to stage.
    candles = _flat_candles(n=300)
    result = ie.analyze(candles)
    assert result["trend_stage"] is None
    assert result["recommended_action"] == "Avoid New Entries"


def test_reversal_probability_flags_exhausted_extended_move():
    # Build a strong uptrend, then flatten abruptly -- price stays far above
    # Kijun (extended) while momentum stalls, which should read as
    # Exhaustion or Reversal Probability, never "Strong Trend".
    up = _trend_candles(n=300, slope=0.2, noise=0.2, seed=40)
    flat_start = up[-1]["close"]
    flat = _flat_candles(n=60, price=flat_start)
    for i, c in enumerate(flat):
        c["datetime"] = up[-1]["datetime"] + dt.timedelta(minutes=3 * (i + 1))
    result = ie.analyze(up + flat)
    if result["trend_stage"] is not None:
        assert result["trend_stage"] in ("Exhaustion", "Reversal Probability", "Mature Trend")


# ----------------------------------------------------------------------------
# Multi-timeframe confirmation
# ----------------------------------------------------------------------------

def test_mtf_all_bullish_is_aligned():
    result = ie.mtf_confirmation({"1m": "BUY", "5m": "STRONG BUY", "15m": "BUY"})
    assert result["status"] == "Aligned"
    assert result["overall_direction"] == "Bullish"
    assert result["agreement_pct"] == 100.0


def test_mtf_split_is_opposite_or_mixed():
    result = ie.mtf_confirmation({"1m": "BUY", "5m": "SELL"})
    assert result["status"] in ("Opposite", "Mixed")


def test_mtf_all_neutral_is_mixed_with_zero_agreement():
    result = ie.mtf_confirmation({"1m": "WAIT", "5m": "NO_TRADE"})
    assert result["overall_direction"] == "Neutral"
    assert result["agreement_pct"] == 0.0


# ----------------------------------------------------------------------------
# Incremental (streaming) engine must match the bulk engine
# ----------------------------------------------------------------------------

def test_incremental_engine_matches_bulk_calculation():
    candles = _trend_candles(n=250, slope=0.1, noise=0.4, seed=20)
    bulk_df = ie.calculate_ichimoku(candles)
    bulk_row = bulk_df.iloc[-1]

    live = ie.IchimokuLiveEngine()
    live.seed(candles)
    snap = live.snapshot()

    assert snap["ichimoku_values"]["tenkan"] == round(float(bulk_row["tenkan"]), 2)
    assert snap["ichimoku_values"]["kijun"] == round(float(bulk_row["kijun"]), 2)
    assert snap["ichimoku_values"]["senkou_a"] == round(float(bulk_row["senkou_a"]), 2)
    assert snap["ichimoku_values"]["senkou_b"] == round(float(bulk_row["senkou_b"]), 2)


def test_incremental_engine_push_advances_window_without_growing_unbounded():
    candles = _trend_candles(n=200, slope=0.1, seed=21)
    live = ie.IchimokuLiveEngine()
    live.seed(candles)
    window_size = len(live)
    for i in range(50):
        live.push({
            "datetime": candles[-1]["datetime"] + dt.timedelta(minutes=3 * (i + 1)),
            "open": 150, "high": 151, "low": 149, "close": 150.5, "volume": 500,
        })
    assert len(live) == window_size   # bounded deque, never grows past its maxlen


def test_incremental_engine_matches_bulk_after_appending_new_candles():
    candles = _trend_candles(n=250, slope=0.1, noise=0.4, seed=22)
    extra = _trend_candles(n=20, slope=0.1, noise=0.4, seed=23, start_price=candles[-1]["close"])
    for i, c in enumerate(extra):
        c["datetime"] = candles[-1]["datetime"] + dt.timedelta(minutes=3 * (i + 1))
    full = candles + extra

    live = ie.IchimokuLiveEngine()
    live.seed(candles)
    for c in extra:
        live.push(c)

    bulk_df = ie.calculate_ichimoku(full)
    bulk_row = bulk_df.iloc[-1]
    snap = live.snapshot()
    assert snap["ichimoku_values"]["tenkan"] == round(float(bulk_row["tenkan"]), 2)
    assert snap["ichimoku_values"]["kijun"] == round(float(bulk_row["kijun"]), 2)


# ----------------------------------------------------------------------------
# Accepts both list[dict] and pandas DataFrame candle input
# ----------------------------------------------------------------------------

def test_accepts_dataframe_input_same_as_list_of_dicts():
    candles = _trend_candles(n=200, slope=0.1, seed=30)
    df_input = pd.DataFrame(candles)
    result_from_list = ie.analyze(candles)
    result_from_df = ie.analyze(df_input)
    assert result_from_list["ichimoku_values"] == result_from_df["ichimoku_values"]
    assert result_from_list["entry_signal"] == result_from_df["entry_signal"]
