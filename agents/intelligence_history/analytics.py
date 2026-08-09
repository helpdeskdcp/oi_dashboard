"""
agents/intelligence_history/analytics.py -- Milestone 13, Phase 2: reads
intelligence_snapshots_log history and computes drift/stability
statistics. Operates ONLY on rows already logged by
intelligence_history_cli.py and, for compute_bias_price_correlation(),
already-archived candles (agents.trading_intelligence.data_access.
load_candles(), the SAME accessor agents/shadow_mode/evaluator.py and
intelligence_orchestrator.py both already use) -- never a live fetch,
never a broker call, zero writes anywhere.

Called manually or by a test/future API action -- nothing in this module
is wired into the scheduler, app.py startup, or any background thread.
"""
import datetime as dt
import statistics

from agents.trading_intelligence import data_access

from . import store

_RESOLVED_GREEKS = {"BULLISH LEAN", "BEARISH LEAN"}


def compute_bias_stability(*, symbol: str, since_ts: str | None = None) -> dict:
    """Flip rate: how often consecutive logged snapshots for `symbol`
    changed `bias`, out of total consecutive transitions. A high flip
    rate means the orchestrator's bias call is noisy for this symbol
    over the observed window."""
    rows = store.list_since(symbol=symbol, since_ts=since_ts)
    if len(rows) < 2:
        return {"snapshot_count": len(rows), "transitions": 0, "flips": 0, "flip_rate": None}
    transitions = len(rows) - 1
    flips = sum(1 for i in range(1, len(rows)) if rows[i]["bias"] != rows[i - 1]["bias"])
    return {
        "snapshot_count": len(rows), "transitions": transitions, "flips": flips,
        "flip_rate": round(flips / transitions, 4),
    }


def compute_confidence_stability(*, symbol: str, since_ts: str | None = None) -> dict:
    """Mean/stdev of confidence across the logged window, plus the
    largest single consecutive-snapshot jump -- a proxy for how noisy
    vs. how smoothly-trending the orchestrator's confidence score is."""
    rows = store.list_since(symbol=symbol, since_ts=since_ts)
    confidences = [r["confidence"] for r in rows if r["confidence"] is not None]
    if len(confidences) < 2:
        return {
            "snapshot_count": len(rows),
            "mean_confidence": confidences[0] if confidences else None,
            "stdev": None, "max_single_step_delta": None,
        }
    deltas = [abs(confidences[i] - confidences[i - 1]) for i in range(1, len(confidences))]
    return {
        "snapshot_count": len(rows),
        "mean_confidence": round(statistics.mean(confidences), 4),
        "stdev": round(statistics.stdev(confidences), 4),
        "max_single_step_delta": max(deltas),
    }


def compute_greeks_coherence(*, symbol: str, since_ts: str | None = None) -> dict:
    """% of snapshots where a RESOLVED bias (BULLISH/BEARISH) and a
    RESOLVED greeks_alignment (BULLISH LEAN/BEARISH LEAN) actually agree.
    bias comes from compute_trend_meter()'s zone; greeks_alignment comes
    from generate_signal()'s own direction/delta_used -- genuinely
    independent signals inside intelligence_orchestrator.build_snapshot(),
    so persistent disagreement here is a real finding, not expected
    noise. NEUTRAL bias and UNAVAILABLE/NEUTRAL greeks_alignment rows are
    excluded (nothing to agree/disagree about), never counted against
    the rate."""
    rows = store.list_since(symbol=symbol, since_ts=since_ts)
    resolved = [r for r in rows if r["bias"] in ("BULLISH", "BEARISH") and r["greeks_alignment"] in _RESOLVED_GREEKS]
    if not resolved:
        return {"snapshot_count": len(rows), "resolved_count": 0, "coherent_count": 0, "coherence_rate": None}
    expected = {"BULLISH": "BULLISH LEAN", "BEARISH": "BEARISH LEAN"}
    coherent = sum(1 for r in resolved if r["greeks_alignment"] == expected[r["bias"]])
    return {
        "snapshot_count": len(rows), "resolved_count": len(resolved), "coherent_count": coherent,
        "coherence_rate": round(coherent / len(resolved), 4),
    }


def compute_oi_responsiveness(*, symbol: str, since_ts: str | None = None) -> dict:
    """How often oi_strength actually changes between consecutive logged
    snapshots. A flat oi_strength across many logged snapshots would
    mean the orchestrator isn't picking up fresh option-chain data for
    this symbol (or the underlying signal genuinely hasn't moved) --
    this is a diagnostic, not a correctness judgment."""
    rows = store.list_since(symbol=symbol, since_ts=since_ts)
    values = [r["oi_strength"] for r in rows if r["oi_strength"] is not None]
    if len(values) < 2:
        return {"snapshot_count": len(rows), "transitions": 0, "changed": 0, "change_rate": None}
    transitions = len(values) - 1
    changed = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1])
    return {
        "snapshot_count": len(rows), "transitions": transitions, "changed": changed,
        "change_rate": round(changed / transitions, 4),
    }


def compute_bias_price_correlation(*, symbol: str, since_ts: str | None = None,
                                     lookforward_minutes: int = 15) -> dict:
    """For each logged snapshot with a directional bias (BULLISH/
    BEARISH), checks whether the underlying's close price actually moved
    in the bias-implied direction over the following `lookforward_minutes`
    -- reusing the exact already-archived-candle read
    (agents.trading_intelligence.data_access.load_candles()) agents/
    shadow_mode/evaluator.py's own _classify() already established, but
    simpler: this module has no stored entry_reference_price the way
    shadow_predictions does, so the reference price is honestly derived
    as the most recent archived candle at-or-before the snapshot's own
    timestamp (never fabricated, never the snapshot's own field since
    none is stored). A snapshot whose lookforward window hasn't fully
    elapsed yet, or has no archived candle data covering it, is counted
    as `pending` -- never scored, matching Shadow Mode's own "don't
    fabricate an answer" discipline for the EXPIRED/pending distinction."""
    rows = store.list_since(symbol=symbol, since_ts=since_ts)
    directional = [r for r in rows if r["bias"] in ("BULLISH", "BEARISH")]
    result = {
        "snapshot_count": len(rows), "directional_count": len(directional),
        "scored_count": 0, "agree_count": 0, "agreement_rate": None, "pending_count": 0,
    }
    if not directional:
        return result

    candles = data_access.load_candles(symbol, timeframe=directional[0]["timeframe"])
    now = dt.datetime.now()
    scored = agree = pending = 0

    for row in directional:
        snap_ts = dt.datetime.fromisoformat(row["ts"])
        horizon = snap_ts + dt.timedelta(minutes=lookforward_minutes)
        if now < horizon or candles is None or candles.empty:
            pending += 1
            continue

        before = candles[candles["datetime"] <= snap_ts]
        window = candles[(candles["datetime"] > snap_ts) & (candles["datetime"] <= horizon)]
        if before.empty or window.empty:
            pending += 1
            continue

        entry_price = before.iloc[-1]["close"]
        exit_price = window.iloc[-1]["close"]
        moved_up = exit_price > entry_price
        agrees = (moved_up and row["bias"] == "BULLISH") or (not moved_up and row["bias"] == "BEARISH")

        scored += 1
        if agrees:
            agree += 1

    result["scored_count"] = scored
    result["agree_count"] = agree
    result["pending_count"] = pending
    result["agreement_rate"] = round(agree / scored, 4) if scored else None
    return result


def compute_report(*, symbol: str, since_ts: str | None = None) -> dict:
    """Bundles all five statistics into one "historical observation
    report" -- the data source behind GET /api/intelligence/history/report
    and `intelligence_history_cli.py report`."""
    return {
        "symbol": symbol,
        "since_ts": since_ts,
        "bias_stability": compute_bias_stability(symbol=symbol, since_ts=since_ts),
        "confidence_stability": compute_confidence_stability(symbol=symbol, since_ts=since_ts),
        "greeks_coherence": compute_greeks_coherence(symbol=symbol, since_ts=since_ts),
        "oi_responsiveness": compute_oi_responsiveness(symbol=symbol, since_ts=since_ts),
        "bias_price_correlation": compute_bias_price_correlation(symbol=symbol, since_ts=since_ts),
    }
