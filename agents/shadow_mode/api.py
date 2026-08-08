"""
agents/shadow_mode/api.py -- Milestone 12, Phase 2B: read-only
aggregation functions backing the three GET-only /api/shadow/* routes
in app.py. Every function here only reads (store.py's own SELECT-only
helpers + evaluator.compute_metrics(), itself read-only) -- nothing in
this module writes anything, matching "no POST/PUT/PATCH/DELETE
endpoint in this phase."
"""
from . import evaluator, store


def get_status() -> dict:
    return {
        "mode": "shadow",
        "read_only": True,
        "no_orders_placed": True,
        "observation_count": store.count_observations(),
        "prediction_count": store.count_predictions(),
        "last_prediction_ts": store.last_prediction_ts(),
    }


def get_recent(*, symbol: str | None = None, limit: int = 10) -> list:
    return store.list_recent_predictions_with_outcomes(symbol=symbol, limit=limit)


def get_performance(*, symbol: str | None = None, since_ts: str | None = None) -> dict:
    return evaluator.compute_metrics(symbol=symbol, since_ts=since_ts)
