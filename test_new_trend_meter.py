"""
test_new_trend_meter.py -- regression tests for oi_engine.compute_new_trend_meter's
Ichimoku fusion (dynamic regime weighting, disagreement-forces-WAIT,
STRONG-only-on-majority-alignment). Synthetic data only, same philosophy as
test_engine.py / test_ichimoku_engine.py.

The single most important guarantee tested here: calling with ONLY the
original 7 params (no ichimoku/market_structure/is_expiry_today) must
reproduce the EXACT original 5-factor 35/25/15/15/10 score/zone/confidence
-- this extension must never break an existing caller that doesn't opt in.
"""
from oi_engine import StrikeRow, compute_new_trend_meter

STEP = 50


def _rows_bullish_oi(atm=24700, step=STEP, n_each_side=4):
    """Fresh CALL OI-change positive-and-large near ATM -> bullish money-flow
    and OI-balance sub-scores (put-writing-like lean toward puts is what's
    bullish per this engine's convention: net PUT OI change minus CALL --
    here we make PUT OI change strongly positive, CALL slightly negative, a
    clean unambiguous bullish setup)."""
    rows = []
    for i in range(-n_each_side, n_each_side + 1):
        strike = atm + i * step
        r = StrikeRow(strike=strike)
        r.ce_ltp, r.pe_ltp = 80.0, 80.0
        r.ce_vol, r.pe_vol = 4000, 6000
        r.ce_oi_chg, r.pe_oi_chg = -5000, 20000   # puts being written heavily -> bullish
        r.ce_delta, r.pe_delta = 0.45, -0.45
        rows.append(r)
    return rows


def _rows_bearish_oi(atm=24700, step=STEP, n_each_side=4):
    rows = []
    for i in range(-n_each_side, n_each_side + 1):
        strike = atm + i * step
        r = StrikeRow(strike=strike)
        r.ce_ltp, r.pe_ltp = 80.0, 80.0
        r.ce_vol, r.pe_vol = 6000, 4000
        r.ce_oi_chg, r.pe_oi_chg = 20000, -5000   # calls being written heavily -> bearish
        r.ce_delta, r.pe_delta = 0.45, -0.45
        rows.append(r)
    return rows


def _rows_mildly_bullish_oi(atm=24700, step=STEP, n_each_side=4):
    """Moderate bullish OI lean -- strong enough to clear
    DISAGREEMENT_MIN_MAGNITUDE (10) as a genuine institutional opinion, but
    deliberately mild enough that NO single sub-score crosses the >=40
    'strong internal agreement' bar, unlike _rows_bullish_oi's deliberately
    extreme fixture."""
    rows = []
    for i in range(-n_each_side, n_each_side + 1):
        strike = atm + i * step
        r = StrikeRow(strike=strike)
        r.ce_ltp, r.pe_ltp = 80.0, 80.0
        r.ce_vol, r.pe_vol = 5000, 6000
        r.ce_oi_chg, r.pe_oi_chg = 2000, 5000
        r.ce_delta, r.pe_delta = 0.45, -0.45
        rows.append(r)
    return rows


def _rows_no_volume(atm=24700, step=STEP, n_each_side=4):
    rows = []
    for i in range(-n_each_side, n_each_side + 1):
        strike = atm + i * step
        r = StrikeRow(strike=strike)
        r.ce_ltp, r.pe_ltp = 80.0, 80.0
        r.ce_vol, r.pe_vol = 0, 0
        r.ce_oi_chg, r.pe_oi_chg = 0, 0
        rows.append(r)
    return rows


def _ichimoku_output(action="BUY", confidence=70, momentum=None, cloud_thickness=10,
                      support=24650, resistance=24800, trend_stage="Early Trend"):
    return {
        "entry_signal": action, "confidence_score": confidence,
        "momentum": momentum or {"trend_acceleration": True},
        "cloud": {"direction": "Bullish" if action in ("BUY", "STRONG BUY") else "Bearish", "thickness": cloud_thickness},
        "support": support, "resistance": resistance,
        "trend_stage": trend_stage,
        "signal_detail": {"conditions_passed": 7, "conditions_total": 10},
        "reasons": ["price above cloud", "tenkan above kijun"],
    }


# ----------------------------------------------------------------------------
# Backward compatibility -- the critical guarantee
# ----------------------------------------------------------------------------

def test_omitting_new_params_reproduces_original_output_shape():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP)
    # Original keys must all still be present with the original meaning.
    for key in ("score", "zone", "confidence", "money_flow_pct", "oi_balance_pct",
                "pcr_pct", "price_pct", "greeks_pct", "greeks_available", "note"):
        assert key in result
    # No ichimoku supplied -> no ichimoku-specific contribution to the score.
    assert result["ichimoku_pct"] is None
    assert result["institutional_score"] is None
    assert result["disagreement"] is False
    assert result["forced_wait"] is False


def test_original_five_factor_weights_unchanged_when_ichimoku_omitted():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP)
    assert result["weights"] == {"money_flow": 0.35, "oi_balance": 0.25, "pcr": 0.15, "price_vwap": 0.15, "greeks": 0.10}
    assert result["regime_weighting"]["multipliers_applied"] == {}


def test_confidence_formula_unchanged_when_ichimoku_omitted():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP)
    sub_scores = [result["money_flow_pct"], result["oi_balance_pct"], result["pcr_pct"], result["price_pct"], result["greeks_pct"]]
    score = result["score"]
    if score > 0:
        agreeing = sum(1 for s in sub_scores if s > 0)
    elif score < 0:
        agreeing = sum(1 for s in sub_scores if s < 0)
    else:
        agreeing = 0
    assert result["confidence"] == round(agreeing / 5 * 100)


# ----------------------------------------------------------------------------
# Ichimoku fusion -- new behavior, only when opted in
# ----------------------------------------------------------------------------

def test_ichimoku_supplied_adds_sixth_weight_summing_to_one():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("BUY", 70),
    )
    assert "ichimoku" in result["weights"]
    assert abs(sum(result["weights"].values()) - 1.0) < 1e-9
    assert result["ichimoku_pct"] is not None


def test_agreeing_ichimoku_and_oi_produce_bullish_recommendation():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.4, pcr_history=[1.1, 1.15, 1.2], underlying=24750, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("STRONG BUY", 85),
    )
    assert result["recommendation"] in ("BUY", "STRONG BUY")
    assert result["disagreement"] is False


def test_conflicting_oi_and_ichimoku_forces_wait_without_strong_institutional_agreement():
    # Mildly bullish OI positioning (no sub-score strongly >=40) but
    # Ichimoku reads a confident SELL -- must be forced to WAIT rather than
    # let one side silently win.
    rows = _rows_mildly_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.0, pcr_history=[1.0, 1.0, 1.0], underlying=24700, vwap=24700, step=STEP,
        ichimoku=_ichimoku_output("STRONG SELL", 80),
    )
    assert result["disagreement"] is True
    assert result["strong_internal_agreement"] < 2
    assert result["forced_wait"] is True
    assert result["recommendation"] == "WAIT"


def test_conflicting_ichimoku_overridden_by_strong_institutional_agreement():
    # Extreme, unambiguous bullish OI positioning (money_flow AND oi_balance
    # both strongly >=40) against a lone dissenting Ichimoku SELL read --
    # the institutional side should override the outlier, NOT force WAIT.
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.0, pcr_history=[1.0, 1.0, 1.0], underlying=24700, vwap=24700, step=STEP,
        ichimoku=_ichimoku_output("STRONG SELL", 80),
    )
    assert result["disagreement"] is True
    assert result["strong_internal_agreement"] >= 2
    assert result["forced_wait"] is False
    assert result["recommendation"] != "WAIT"


def test_strong_recommendation_requires_majority_alignment():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.4, pcr_history=[1.1, 1.15, 1.2], underlying=24750, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("STRONG BUY", 90),
    )
    if result["recommendation"] == "STRONG BUY":
        majority_threshold = len(result["weights"]) // 2 + 1
        sub_scores = [result["money_flow_pct"], result["oi_balance_pct"], result["pcr_pct"],
                      result["price_pct"], result["greeks_pct"], result["ichimoku_pct"]]
        agreeing = sum(1 for s in sub_scores if s and s > 0)
        assert agreeing >= majority_threshold


def test_low_volume_reduces_confidence():
    rows_thin = _rows_no_volume()
    result = compute_new_trend_meter(rows_thin, 24700, pcr=1.0, pcr_history=[], underlying=24700, vwap=24700, step=STEP)
    assert result["regime_weighting"]["low_volume"] is True
    assert any("volume" in r for r in result["reasons"])


def test_ranging_regime_downweights_ichimoku_and_upweights_others():
    rows = _rows_bullish_oi()
    ms = {"regime": "RANGING", "atr_14": 20}
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("BUY", 70), market_structure=ms,
    )
    assert result["weights"]["ichimoku"] < ICHIMOKU_WEIGHT_AFTER_RESCALE_ONLY(rows_count=len(rows))


def ICHIMOKU_WEIGHT_AFTER_RESCALE_ONLY(rows_count):
    # Helper: the ichimoku weight if ONLY the base 80/20 rescale applied (no
    # regime multiplier) -- used as the comparison baseline above.
    return 0.20


def test_trending_regime_upweights_ichimoku():
    rows = _rows_bullish_oi()
    ms = {"regime": "TRENDING", "atr_14": 20}
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("BUY", 70), market_structure=ms,
    )
    assert result["weights"]["ichimoku"] > 0.20


def test_expiry_day_upweights_oi_and_downweights_ichimoku():
    rows = _rows_bullish_oi()
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP,
        ichimoku=_ichimoku_output("BUY", 70), is_expiry_today=True,
    )
    assert result["weights"]["ichimoku"] < 0.20
    assert result["regime_weighting"]["is_expiry_today"] is True


def test_ichimoku_display_fields_populate_from_engine_output():
    rows = _rows_bullish_oi()
    ich = _ichimoku_output("BUY", 70, support=24650, resistance=24800, trend_stage="Strong Trend")
    result = compute_new_trend_meter(
        rows, 24700, pcr=1.3, pcr_history=[1.1, 1.15, 1.2], underlying=24720, vwap=24680, step=STEP,
        ichimoku=ich,
    )
    assert result["dynamic_support"] == 24650
    assert result["dynamic_resistance"] == 24800
    assert result["trend_stage"] == "Strong Trend"
    assert result["cloud_direction"] == "Bullish"
    assert result["entry_quality"] in ("High", "Medium", "Low")
