"""
test_agents/quant_researcher/test_hypotheses.py -- regression tests for
the declarative HYPOTHESIS_CATALOG and its spec builders.
"""
import pytest

from agents.quant_researcher import hypotheses


def test_catalog_covers_all_ten_requested_research_ideas():
    expected_categories = {
        "OI + Delta combinations", "VWAP + Gamma", "ATR + Premium Expansion",
        "Max Pain Behaviour", "CPR + Institutional Activity", "IV Crush",
        "Expiry behaviour", "Range breakout probability", "Momentum exhaustion",
        "Liquidity sweep detection",
    }
    actual_categories = {h["category"] for h in hypotheses.HYPOTHESIS_CATALOG}
    assert actual_categories == expected_categories


def test_every_catalog_feature_name_is_registered():
    from agents.quant_researcher.features import FEATURE_REGISTRY
    for h in hypotheses.HYPOTHESIS_CATALOG:
        for name in h["features"]:
            assert name in FEATURE_REGISTRY, f"{h['id']} references unregistered feature {name!r}"


def test_build_spec_merges_default_and_override_thresholds():
    spec = hypotheses.build_spec(
        "oi_delta_combo", symbol="NIFTY", target_points=20, stop_points=10,
        thresholds={"oi_delta_bias": 5.0},
    )
    assert spec.thresholds["oi_delta_bias"] == 5.0
    assert spec.symbol == "NIFTY"
    assert spec.hypothesis_id == "oi_delta_combo"


def test_build_spec_unknown_id_raises():
    with pytest.raises(KeyError):
        hypotheses.build_spec("not_a_real_id", symbol="NIFTY", target_points=1, stop_points=1)


def test_generate_hypotheses_excludes_ids():
    specs = hypotheses.generate_hypotheses(
        symbol="NIFTY", target_points=10, stop_points=5, exclude_ids={"iv_crush", "momentum_exhaustion"},
    )
    ids = {s.hypothesis_id for s in specs}
    assert "iv_crush" not in ids
    assert "momentum_exhaustion" not in ids
    assert len(specs) == len(hypotheses.HYPOTHESIS_CATALOG) - 2


def test_combine_hypotheses_merges_features_and_thresholds():
    a = hypotheses.build_spec("max_pain_behaviour", symbol="NIFTY", target_points=10, stop_points=5)
    b = hypotheses.build_spec("iv_crush", symbol="NIFTY", target_points=10, stop_points=5)
    combined = hypotheses.combine_hypotheses(a, b)
    assert set(combined.features) == {"max_pain_distance", "iv_crush"}
    assert combined.thresholds["max_pain_distance"] == a.thresholds["max_pain_distance"]
    assert combined.thresholds["iv_crush"] == b.thresholds["iv_crush"]
    assert combined.hypothesis_id == "max_pain_behaviour+iv_crush"
