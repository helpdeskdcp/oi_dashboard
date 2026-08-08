"""
agents/shadow_mode/api.py -- Milestone 12, Phase 2B: read-only
aggregation functions backing the three GET-only /api/shadow/* routes
in app.py. Every function here only reads (store.py's own SELECT-only
helpers + evaluator.compute_metrics(), itself read-only) -- nothing in
this module writes anything, matching "no POST/PUT/PATCH/DELETE
endpoint in this phase."
"""
import datetime as dt

from . import evaluator, store


def _start_of_today_iso() -> str:
    """Same naive-datetime, server-local-time convention observer.py's
    own record_observation()/record_prediction() calls already use
    (dt.datetime.now().isoformat()) -- "today" here means the same
    clock those rows were timestamped against, not a UTC/IST
    reinterpretation of it."""
    return dt.datetime.combine(dt.date.today(), dt.time.min).isoformat()


def get_status() -> dict:
    today_start = _start_of_today_iso()
    return {
        "mode": "shadow",
        "read_only": True,
        "no_orders_placed": True,
        "observation_count": store.count_observations(),
        "prediction_count": store.count_predictions(),
        "last_prediction_ts": store.last_prediction_ts(),
        "observations_today": store.count_observations_since(today_start),
        "predictions_today": store.count_predictions_since(today_start),
        "evaluated_outcomes_today": store.count_evaluated_outcomes_since(today_start),
        "current_win_rate": evaluator.compute_metrics()["win_rate"],
    }


def get_recent(*, symbol: str | None = None, limit: int = 10) -> list:
    return store.list_recent_predictions_with_outcomes(symbol=symbol, limit=limit)


def get_performance(*, symbol: str | None = None, since_ts: str | None = None) -> dict:
    return evaluator.compute_metrics(symbol=symbol, since_ts=since_ts)
