"""
agents/shadow_mode/observer.py -- Milestone 12, Phase 2B: computes one
hypothetical signal from already-stored data and records it as a
shadow_observations + shadow_predictions row pair.

Deliberately reuses the LOW-LEVEL, pure signal primitives rather than
agents.trading_intelligence.ai_trading_engine.evaluate() -- evaluate()
has a real side effect (ti_store.close_trade() when an existing open
ti_paper_trades position's exit condition is hit) that this module must
never be able to trigger. Every function called from here was verified
side-effect-free before being used:

- market_data.get_snapshot(): "aggregated from already-stored data
  only" (its own docstring) -- reads cycles/strikes, no broker call.
- oi_engine.oi_walls() / detect_bias() / generate_signal(): pure
  functions, zero DB access (grepped for INSERT/UPDATE/DELETE/.execute/
  conn./sqlite3/_store. across generate_signal's full body -- zero
  matches). generate_signal's own docstring: "Rule-based signal: CE/PE
  direction, strike, entry, target, SL, confidence. NOT ML, NOT
  guaranteed" -- the SAME function app.py's live dashboard already runs
  every cycle, and the SAME one ai_trading_engine.evaluate() itself
  wraps.
- data_access.load_candles() / latest_market_structure(): read already-
  archived files/rows only.

No entrypoint in this module is called from app.py's startup, the
scheduler, or any background thread -- observe_and_predict() is meant
to be invoked manually or by a test/future API action, matching this
phase's "100% passive" scope.
"""
import datetime as dt
import json

from oi_engine import detect_bias as oi_engine_detect_bias
from oi_engine import generate_signal as oi_engine_generate_signal
from oi_engine import oi_walls as oi_engine_oi_walls

from agents.trading_intelligence import data_access, market_data

from . import store

DEFAULT_TIMEFRAME = "3m"
DEFAULT_VALID_MINUTES = 45   # how long a prediction stays "pending" before evaluator.py treats it as expired


def _price_trend_pct(candles) -> float | None:
    """Same cheap momentum proxy as ai_trading_engine._price_trend_pct
    (last 5 candles' % close change) -- reimplemented locally (a few
    lines) rather than importing that module's private underscore-
    prefixed function, keeping this package decoupled from
    trading_intelligence's internals."""
    if candles is None or candles.empty or len(candles) < 6:
        return None
    recent = candles.tail(6)
    start, end = recent.iloc[0]["close"], recent.iloc[-1]["close"]
    if not start:
        return None
    return round((end - start) / start * 100, 4)


def compute_observation_and_prediction(symbol: str, *, timeframe: str = DEFAULT_TIMEFRAME,
                                        expiry_date: dt.date | None = None,
                                        valid_minutes: int = DEFAULT_VALID_MINUTES,
                                        now: dt.datetime | None = None) -> dict | None:
    """Pure computation -- runs the exact same market-snapshot read and
    signal-generation primitives observe_and_predict() uses, but performs
    ZERO database writes (no store.record_observation()/record_prediction()
    call anywhere in this function). Returns
    {"observation": {...columns...}, "prediction": {...columns...}, "signal": {...}}
    ready to be persisted by the caller, printed, or exported to JSON --
    or None if there's no usable market snapshot yet. Shared by
    observe_and_predict() (which persists the result) and the CLI's
    --dry-run mode (which only displays/exports it)."""
    now = now or dt.datetime.now()
    snapshot = market_data.get_snapshot(symbol, expiry_date=expiry_date)
    if not snapshot.available or not snapshot.strikes or snapshot.atm is None or snapshot.pcr is None:
        return None

    candles = data_access.load_candles(symbol, timeframe=timeframe)
    price_trend_pct = _price_trend_pct(candles)
    market_structure = data_access.latest_market_structure(symbol)
    market_bias, bias_note = oi_engine_detect_bias(
        snapshot.strikes, snapshot.atm, snapshot.pcr, price_trend_pct, snapshot.underlying_ltp, market_structure,
    )

    observation = {
        "ts": now.isoformat(), "symbol": symbol, "timeframe": timeframe,
        "underlying_ltp": snapshot.underlying_ltp, "atm": snapshot.atm, "pcr": snapshot.pcr,
        "market_bias": market_bias, "bias_note": bias_note,
        "context_json": json.dumps({"price_trend_pct": price_trend_pct, "as_of_ts": snapshot.as_of_ts}),
    }

    support, resistance = oi_engine_oi_walls(snapshot.strikes)
    signal = oi_engine_generate_signal(
        snapshot.strikes, snapshot.atm, market_bias, bias_note, snapshot.pcr, support, resistance,
        underlying=snapshot.underlying_ltp, expiry_date=expiry_date, market_structure=market_structure,
    )

    entry_price = signal.get("entry_price")
    target_price = signal.get("target_price")
    target_low, target_high = None, None
    if entry_price is not None and target_price is not None:
        target_low, target_high = sorted((entry_price, target_price))

    valid_until = now + dt.timedelta(minutes=valid_minutes)
    prediction = {
        "ts": now.isoformat(), "symbol": symbol, "timeframe": timeframe,
        "signal_type": signal.get("action", "NO_TRADE"), "expected_direction": signal.get("direction"),
        "confidence": signal.get("confidence"), "reasoning_snapshot": signal.get("reason"),
        "entry_reference_price": entry_price if entry_price is not None else snapshot.underlying_ltp,
        "expected_target_low": target_low, "expected_target_high": target_high,
        "valid_until_ts": valid_until.isoformat(),
    }
    return {"observation": observation, "prediction": prediction, "signal": signal}


def observe_and_predict(symbol: str, *, timeframe: str = DEFAULT_TIMEFRAME,
                         expiry_date: dt.date | None = None,
                         valid_minutes: int = DEFAULT_VALID_MINUTES) -> dict | None:
    """One observe-then-predict cycle for `symbol`. Returns
    {"observation_id", "prediction_id", "signal"} on a tradeable-or-not
    signal, or None if there's no usable market snapshot yet (never
    raises for a data-availability reason, matching every other reader
    in this framework). Read-only against everything except this
    package's own two tables."""
    computed = compute_observation_and_prediction(
        symbol, timeframe=timeframe, expiry_date=expiry_date, valid_minutes=valid_minutes,
    )
    if computed is None:
        return None

    observation_id = store.record_observation(**computed["observation"])
    prediction_id = store.record_prediction(observation_id=observation_id, **computed["prediction"])
    return {"observation_id": observation_id, "prediction_id": prediction_id, "signal": computed["signal"]}
