"""
agents/trading_supervisor/conflict_detector.py -- "Detect conflicting
signals between strategies." Compares the direction every currently-
promoted ("is_best") strategy on a symbol is trading against a
candidate's own direction: a same-symbol, opposite-direction pair is a
real conflict (one strategy's long signal directly fights another's
short signal on the same underlying), surfaced, never silently ignored.
"""
import dataclasses

_OPPOSITES = {"long": "short", "short": "long"}


@dataclasses.dataclass
class Conflict:
    symbol: str
    strategy_a: str
    direction_a: str
    strategy_b: str
    direction_b: str


def active_strategy_directions(memory_store, *, exclude_strategy_name: str | None = None, limit: int = 1000) -> list:
    """Every currently-promoted strategy's (strategy_name, symbol,
    direction). direction comes from the `direction` key
    agents.quant_researcher.research_engine stores inside
    record_parameter_set's `parameters` payload (Milestone 7) -- a
    strategy promoted before this milestone has no `direction` key and
    is skipped: "unknown direction" is not evidence of a conflict, only
    a known opposite direction is."""
    rows = memory_store.search_parameter_sets(limit=limit)
    result = []
    for r in rows:
        if not r.get("is_best"):
            continue
        if exclude_strategy_name and r.get("strategy_name") == exclude_strategy_name:
            continue
        direction = (r.get("parameters") or {}).get("direction")
        if direction not in ("long", "short"):
            continue
        result.append({"strategy_name": r.get("strategy_name"), "symbol": r.get("symbol"), "direction": direction})
    return result


def detect_conflicts(candidate_name: str, symbol: str, direction: str, active_strategies: list) -> list:
    if direction not in ("long", "short"):
        return []  # a "both"-direction candidate isn't a one-sided bet to conflict with anything
    conflicts = []
    for s in active_strategies:
        if s["symbol"] != symbol:
            continue
        if s["direction"] == _OPPOSITES[direction]:
            conflicts.append(Conflict(
                symbol=symbol, strategy_a=candidate_name, direction_a=direction,
                strategy_b=s["strategy_name"], direction_b=s["direction"],
            ))
    return conflicts
