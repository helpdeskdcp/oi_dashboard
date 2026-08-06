"""
agents/quant_researcher/hypotheses.py -- HYPOTHESIS_CATALOG: the research
ideas a cycle starts from. Purely declarative (id, category, which
FEATURE_REGISTRY features to combine, a default direction, default entry
thresholds) -- interpreted generically by strategy_runner.py's
StrategySpec runner, never bespoke code per idea. Extending the catalog
with a new research idea means adding one dict here, not a new module.

Each entry below maps directly to one of the ten research ideas the
Milestone 5 brief named explicitly.
"""
import dataclasses

from .strategy_spec import StrategySpec

HYPOTHESIS_CATALOG = [
    {
        "id": "oi_delta_combo", "category": "OI + Delta combinations",
        "features": ["oi_delta_bias"], "direction": "both",
        "default_thresholds": {"oi_delta_bias": 0.0},
    },
    {
        "id": "vwap_gamma", "category": "VWAP + Gamma",
        "features": ["vwap_deviation", "gamma_exposure"], "direction": "both",
        "default_thresholds": {"vwap_deviation": 0.0015, "gamma_exposure": 0.0},
    },
    {
        "id": "atr_premium_expansion", "category": "ATR + Premium Expansion",
        "features": ["atr", "premium_expansion"], "direction": "long",
        "default_thresholds": {"atr": 0.0, "premium_expansion": 0.02},
    },
    {
        "id": "max_pain_behaviour", "category": "Max Pain Behaviour",
        "features": ["max_pain_distance"], "direction": "both",
        "default_thresholds": {"max_pain_distance": 0.003},
    },
    {
        "id": "cpr_institutional_activity", "category": "CPR + Institutional Activity",
        "features": ["cpr_position", "institutional_activity"], "direction": "both",
        "default_thresholds": {"cpr_position": 0.5, "institutional_activity": 0.0},
    },
    {
        "id": "iv_crush", "category": "IV Crush",
        "features": ["iv_crush"], "direction": "short",
        "default_thresholds": {"iv_crush": -1.0},
    },
    {
        "id": "expiry_behaviour", "category": "Expiry behaviour",
        "features": ["expiry_flag", "iv_crush"], "direction": "both",
        "default_thresholds": {"expiry_flag": 1.0, "iv_crush": -0.5},
    },
    {
        "id": "range_breakout_probability", "category": "Range breakout probability",
        "features": ["range_compression"], "direction": "both",
        "default_thresholds": {"range_compression": 0.6},
        "invert": {"range_compression": True},  # entry when compression < threshold, not >
    },
    {
        "id": "momentum_exhaustion", "category": "Momentum exhaustion",
        "features": ["momentum_exhaustion"], "direction": "both",
        "default_thresholds": {"momentum_exhaustion": 0.3},
    },
    {
        "id": "liquidity_sweep", "category": "Liquidity sweep detection",
        "features": ["liquidity_sweep"], "direction": "both",
        "default_thresholds": {"liquidity_sweep": 0.0},
    },
]

_BY_ID = {h["id"]: h for h in HYPOTHESIS_CATALOG}


def catalog_ids() -> list:
    return [h["id"] for h in HYPOTHESIS_CATALOG]


def build_spec(hypothesis_id: str, *, symbol: str, target_points: float, stop_points: float,
               max_hold_bars: int = 20, thresholds: dict | None = None,
               name: str | None = None) -> StrategySpec:
    h = _BY_ID.get(hypothesis_id)
    if h is None:
        raise KeyError(f"unknown hypothesis_id {hypothesis_id!r}")
    resolved_thresholds = dict(h["default_thresholds"])
    resolved_thresholds.update(thresholds or {})
    return StrategySpec(
        name=name or f"{hypothesis_id}_{symbol}",
        symbol=symbol, hypothesis_id=hypothesis_id, features=list(h["features"]),
        thresholds=resolved_thresholds, direction=h["direction"],
        target_points=target_points, stop_points=stop_points, max_hold_bars=max_hold_bars,
        params={"invert": dict(h.get("invert") or {})},
    )


def generate_hypotheses(*, symbol: str, target_points: float, stop_points: float,
                         max_hold_bars: int = 20, exclude_ids: set | None = None) -> list:
    """One StrategySpec per catalog entry with default thresholds, minus
    any hypothesis_id in exclude_ids -- e.g. ones research_engine.py
    already knows failed for this symbol from Failed Experiment Memory
    ("the AI won't repeat the same mistakes")."""
    exclude_ids = exclude_ids or set()
    return [
        build_spec(h["id"], symbol=symbol, target_points=target_points, stop_points=stop_points,
                   max_hold_bars=max_hold_bars)
        for h in HYPOTHESIS_CATALOG if h["id"] not in exclude_ids
    ]


def combine_hypotheses(spec_a: StrategySpec, spec_b: StrategySpec, *, name: str | None = None) -> StrategySpec:
    """Evolution engine's "combining strategies": merges two specs'
    features/thresholds into one hybrid StrategySpec -- still just data,
    interpreted by the same generic runner."""
    features = list(dict.fromkeys(spec_a.features + spec_b.features))  # de-duplicated, order-preserving
    thresholds = {**spec_a.thresholds, **spec_b.thresholds}
    invert = {**spec_a.params.get("invert", {}), **spec_b.params.get("invert", {})}
    direction = spec_a.direction if spec_a.direction == spec_b.direction else "both"
    return dataclasses.replace(
        spec_a, name=name or f"{spec_a.hypothesis_id}+{spec_b.hypothesis_id}_{spec_a.symbol}",
        hypothesis_id=f"{spec_a.hypothesis_id}+{spec_b.hypothesis_id}",
        features=features, thresholds=thresholds, direction=direction,
        params={"invert": invert},
    )
