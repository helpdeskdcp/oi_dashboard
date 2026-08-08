"""
agents/shadow_mode/evaluator.py -- Milestone 12, Phase 2B: classifies a
recorded shadow_predictions row against already-archived candles
(agents.trading_intelligence.data_access.load_candles(), the SAME
accessor observer.py and ai_trading_engine.evaluate() both already use)
and computes rolling performance metrics. Operates ONLY on historical
data already sitting in data/history/<symbol>/<tf>.parquet -- never a
live fetch, never a broker call.

Called manually or by a test/future API action -- nothing in this
module is wired into the scheduler, app.py startup, or any background
thread.
"""
import datetime as dt
import statistics

from agents.trading_intelligence import data_access

from . import store

CORRECT = "correct"
INCORRECT = "incorrect"
PARTIAL = "partial"
EXPIRED = "expired"
ALL_CLASSIFICATIONS = (CORRECT, INCORRECT, PARTIAL, EXPIRED)


def _direction_sign(direction: str | None) -> int:
    if direction == "CE":
        return 1    # CE = bullish: favorable move is price UP
    if direction == "PE":
        return -1   # PE = bearish: favorable move is price DOWN
    return 0


def evaluate_prediction(prediction_id: int, *, now: dt.datetime | None = None) -> dict | None:
    """Classifies one prediction. Returns the recorded outcome dict, or
    None if the prediction doesn't exist or already has an outcome
    (idempotent -- calling this twice on the same prediction is a safe
    no-op the second time, never a duplicate row: shadow_outcomes.
    prediction_id is UNIQUE)."""
    prediction = store.get_prediction(prediction_id)
    if prediction is None:
        return None
    if store.get_outcome_for_prediction(prediction_id) is not None:
        return None

    now = now or dt.datetime.now()
    direction = prediction.get("expected_direction")
    entry_price = prediction.get("entry_reference_price")
    valid_until_ts = prediction.get("valid_until_ts")
    pred_ts = dt.datetime.fromisoformat(prediction["ts"])
    valid_until = dt.datetime.fromisoformat(valid_until_ts) if valid_until_ts else pred_ts

    if direction not in ("CE", "PE") or entry_price is None:
        # A NO_TRADE / no-direction prediction has nothing to grade
        # against price movement -- record it honestly as expired
        # rather than fabricating a direction to judge.
        outcome_id = store.record_outcome(
            prediction_id=prediction_id, evaluated_ts=now.isoformat(), classification=EXPIRED,
            notes="no directional signal to evaluate (NO_TRADE or missing entry price)",
        )
        return store.get_outcome_for_prediction(prediction_id) or {"id": outcome_id}

    candles = data_access.load_candles(prediction["symbol"], timeframe=prediction["timeframe"])
    if candles is None or candles.empty:
        window = candles
    else:
        window = candles[(candles["datetime"] > pred_ts) & (candles["datetime"] <= valid_until)]

    if window is None or window.empty:
        if now < valid_until:
            # Still within the validity window and no candle data has
            # arrived yet -- not evaluable YET, but also not recorded
            # (caller should try again later; evaluate_pending() will
            # naturally pick it up on a future call since no outcome
            # row was written).
            return None
        outcome_id = store.record_outcome(
            prediction_id=prediction_id, evaluated_ts=now.isoformat(), classification=EXPIRED,
            notes="validity window passed with no archived candle data available to judge it",
        )
        return store.get_outcome_for_prediction(prediction_id) or {"id": outcome_id}

    sign = _direction_sign(direction)
    favorable_extreme = window["high"].max() if sign > 0 else window["low"].min()
    actual_move_pts = (favorable_extreme - entry_price) * sign
    actual_move_pct = round(actual_move_pts / entry_price * 100, 4) if entry_price else None

    target_low, target_high = prediction.get("expected_target_low"), prediction.get("expected_target_high")
    target_distance = abs((target_high - target_low)) if target_low is not None and target_high is not None else None

    last_close = window.iloc[-1]["close"]
    actual_direction = "CE" if last_close >= entry_price else "PE"

    if actual_move_pts <= 0:
        classification = INCORRECT
    elif target_distance is None or actual_move_pts >= target_distance:
        # No target range recorded -> any favorable move at all counts
        # as correct; otherwise correct only once the full entry->target
        # distance was covered. Anything favorable but short of that
        # (including moves below the _PARTIAL_THRESHOLD_FRACTION floor)
        # is still graded PARTIAL, never silently upgraded to correct.
        classification = CORRECT
    else:
        classification = PARTIAL

    outcome_id = store.record_outcome(
        prediction_id=prediction_id, evaluated_ts=now.isoformat(), classification=classification,
        actual_direction=actual_direction, actual_move_pts=round(actual_move_pts, 4), actual_move_pct=actual_move_pct,
        notes=f"evaluated against {len(window)} archived candle(s)",
    )
    result = store.get_outcome_for_prediction(prediction_id)
    return result if result is not None else {"id": outcome_id}


def evaluate_pending(*, limit: int = 100, now: dt.datetime | None = None) -> list:
    """Evaluates every prediction that doesn't have an outcome yet, up
    to `limit`. Returns the list of outcomes actually recorded this
    call (skips predictions still within their validity window with no
    candle data yet -- those stay pending for a future call)."""
    results = []
    for prediction in store.list_predictions_pending_evaluation(limit=limit):
        outcome = evaluate_prediction(prediction["id"], now=now)
        if outcome is not None:
            results.append(outcome)
    return results


def compute_metrics(*, symbol: str | None = None, since_ts: str | None = None) -> dict:
    """Rolling metrics over every EVALUATED prediction (has an
    outcome): win rate, average move captured, confidence calibration
    (mean confidence for correct vs incorrect predictions), and total
    prediction count. Operates entirely on already-recorded rows --
    never re-touches candle data itself (evaluate_pending() already
    did that)."""
    evaluated = store.list_evaluated_predictions(symbol=symbol, since_ts=since_ts)
    total = len(evaluated)
    if total == 0:
        return {
            "prediction_count": 0, "evaluated_count": 0, "win_rate": None,
            "average_move_captured_pct": None, "confidence_calibration": {},
        }

    graded = [p for p in evaluated if p["classification"] in (CORRECT, INCORRECT, PARTIAL)]
    wins = [p for p in graded if p["classification"] == CORRECT]
    win_rate = round(len(wins) / len(graded), 4) if graded else None

    moves = [p["actual_move_pct"] for p in evaluated if p.get("actual_move_pct") is not None]
    average_move_captured_pct = round(statistics.mean(moves), 4) if moves else None

    calibration = {}
    for classification in ALL_CLASSIFICATIONS:
        bucket = [p["confidence"] for p in evaluated if p["classification"] == classification and p.get("confidence") is not None]
        if bucket:
            calibration[classification] = {
                "count": len(bucket), "avg_confidence": round(statistics.mean(bucket), 2),
            }

    return {
        "prediction_count": store.count_predictions(),
        "evaluated_count": total,
        "win_rate": win_rate,
        "average_move_captured_pct": average_move_captured_pct,
        "confidence_calibration": calibration,
    }
