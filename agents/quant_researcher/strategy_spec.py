"""
agents/quant_researcher/strategy_spec.py -- StrategySpec: what a
hypothesis (agents.quant_researcher.hypotheses) becomes once it has
concrete parameters. Purely data -- no method on this class contains
trading logic. strategy_runner.py's generic interpreter is the only
thing that ever reads a StrategySpec and acts on it, which is what makes
this whole package "plug-in based, no hardcoded strategy logic": every
research idea, however different, is the same dataclass shape.
"""
import dataclasses


@dataclasses.dataclass
class StrategySpec:
    name: str
    symbol: str
    hypothesis_id: str
    features: list
    thresholds: dict
    direction: str  # "long", "short", or "both"
    target_points: float
    stop_points: float
    max_hold_bars: int
    params: dict = dataclasses.field(default_factory=dict)
