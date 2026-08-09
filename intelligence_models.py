"""
intelligence_models.py -- Milestone 13, Phase 1: the output shape
intelligence_orchestrator.py produces. A separate module (not folded
into intelligence_orchestrator.py itself) so the shape is importable
without pulling in the orchestrator's own engine-adapter dependencies
-- the same "models file separate from the logic that builds them"
split agents/shadow_mode/store.py's row shapes and its own
observer.py/evaluator.py already establish, just as a plain dataclass
here rather than SQL rows.

NOTE on file location: the Milestone 13 brief requested "backend/
runtime/models.py". No "backend/" directory exists anywhere in this
project -- every standalone, cross-cutting module (oi_engine.py,
greeks.py, sr_probability_engine.py, mcx_session_config.py,
shadow_mode_cli.py) lives at the repo root, matching the project's
one established convention. Placed at the repo root instead, named
"intelligence_models.py" rather than the bare "models.py" requested --
this repo has 30+ top-level modules and no other bare, generic
single-word module name (every one is specific: oi_engine, greeks,
sr_probability_engine, ...), so a bare "models.py" would be the first
of its kind and a likely future collision point. Same call already
made and accepted for mcx_session_config.py's and test_shadow_mode_
cli.py's locations.
"""
import dataclasses


@dataclasses.dataclass
class MarketIntelligenceSnapshot:
    symbol: str
    bias: str                  # "BULLISH" | "BEARISH" | "NEUTRAL" -- always exactly one of these three
    confidence: int            # 0-100, overall aggregate confidence
    oi_strength: int           # 0-100
    probability_score: int     # 0-100
    volume_score: int          # 0-100
    greeks_alignment: str      # e.g. "BULLISH LEAN" | "BEARISH LEAN" | "NEUTRAL" | "UNAVAILABLE"
    institutional_score: int   # 0-100

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
