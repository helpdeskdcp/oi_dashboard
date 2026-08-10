"""
agents/intelligence_alerts/rules.py -- Milestone 14, Phase 1: pure,
read-only rule evaluation against agents.intelligence_history.store's
already-logged intelligence_snapshots_log rows. Zero writes here --
callers (intelligence_alerts_cli.py) decide whether to record/deliver a
triggered rule. Called manually or by a test/API action -- nothing in
this module is wired into the scheduler, app.py startup, or any
background thread.

Each check_*() function returns a {"rule": ..., "detail": ...} dict when
triggered, or None when not (including when there isn't yet enough
history to judge honestly -- never fabricates a trigger from
insufficient data).

Milestone 15, Phase 0: check_bias_flip() also reads this package's own
store.py (read-only -- see its own docstring for why) to apply a
per-rule cooldown. That's still a read, not a write; this module's own
"zero writes here" guarantee is unchanged.
"""
import datetime as dt
import statistics

from agents.intelligence_history import store as history_store
from . import store as alerts_store, threshold_store

# Mirrors agents.intelligence_history.analytics.py's own
# bias -> expected-greeks-alignment mapping (kept local rather than
# importing that module's private constant, to keep this package's
# only dependency on intelligence_history explicit and narrow: just
# store.py's read functions).
_EXPECTED_GREEKS_FOR_BIAS = {"BULLISH": "BULLISH LEAN", "BEARISH": "BEARISH LEAN"}

# Milestone 14 observability pass: the low-liquidity suppression symbol
# set (originally 4 hardcoded symbols confirmed, via live option-chain
# inspection, to genuinely have near-zero real OI/volume) is now
# operator-configurable -- see threshold_store.py's own module
# docstring. Read fresh on every check (not cached at import time) so a
# runtime override via POST /api/intelligence/alerts/config takes
# effect on the very next evaluation, same as every other threshold
# below.
_LOW_LIQUIDITY_QUALITIES = frozenset({"NO_LIQUIDITY", "THIN"})


def check_bias_flip(*, symbol: str) -> dict | None:
    """Milestone 15, Phase 0: Bias Flip Stabilization. A raw single-
    snapshot bias change is no longer enough on its own -- the NEW bias
    has to hold for min_bias_confirmations consecutive logged snapshots
    before this is treated as a real, confirmed flip (set that config
    key to 1 to reproduce the exact pre-Phase-0 behavior: alert on
    every single-snapshot change vs. the immediately prior one). Once
    confirmed, this rule also applies its own bias_flip_cooldown_seconds
    -- see agents/config.py's own comment on both constants for how
    this relates to the separate, generic auto_cooldown_seconds the
    Phase 2 auto-cycle applies at the delivery layer.

    The underlying `bias` value itself is read exactly as logged --
    this function only decides WHEN to alert about a change in it,
    never what that value is."""
    config = threshold_store.get_effective_config()
    min_confirm = config["min_bias_confirmations"]
    rows = history_store.list_recent(symbol=symbol, limit=min_confirm + 1)
    if len(rows) < min_confirm + 1:
        return None  # not enough history yet to confirm a genuine flip

    biases = [r["bias"] for r in rows]  # newest first
    current_bias = biases[0]
    if any(b != current_bias for b in biases[1:min_confirm]):
        return None  # the new bias hasn't held for min_confirm snapshots yet

    prev_bias = biases[min_confirm]
    if prev_bias == current_bias:
        return None  # this confirmed run started before the fetched window -- already alerted on it earlier

    cooldown = config["bias_flip_cooldown_seconds"]
    last_ts = alerts_store.last_alert_ts_for_rule(symbol=symbol, rule="bias_flip")
    if last_ts and (dt.datetime.now() - dt.datetime.fromisoformat(last_ts)).total_seconds() < cooldown:
        return None  # still within this rule's own cooldown

    plural = "" if min_confirm == 1 else "s"
    return {
        "rule": "bias_flip",
        "detail": f"{symbol} bias confirmed: {prev_bias} -> {current_bias} "
                  f"({min_confirm} consecutive snapshot{plural})",
    }


def check_confidence_unstable(*, symbol: str) -> dict | None:
    config = threshold_store.get_effective_config()
    window = config["confidence_window"]
    rows = history_store.list_recent(symbol=symbol, limit=window)
    values = [r["confidence"] for r in rows if r["confidence"] is not None]
    if len(values) < 2:
        return None
    stdev = statistics.stdev(values)
    threshold = config["confidence_stdev_threshold"]
    if stdev >= threshold:
        return {
            "rule": "confidence_unstable",
            "detail": f"{symbol} confidence stdev over last {len(values)} snapshots = "
                      f"{stdev:.1f} (threshold {threshold})",
        }
    return None


def check_greeks_incoherent(*, symbol: str) -> dict | None:
    rows = history_store.list_recent(symbol=symbol, limit=1)
    if not rows:
        return None
    latest = rows[0]
    bias, greeks = latest["bias"], latest["greeks_alignment"]
    expected = _EXPECTED_GREEKS_FOR_BIAS.get(bias)
    if expected and greeks not in (None, "UNAVAILABLE") and greeks != expected:
        return {
            "rule": "greeks_incoherent",
            "detail": f"{symbol} bias={bias} but greeks_alignment={greeks} (expected {expected})",
        }
    return None


def check_oi_non_responsive(*, symbol: str) -> dict | None:
    config = threshold_store.get_effective_config()
    window = config["oi_window"]
    rows = history_store.list_recent(symbol=symbol, limit=window)
    if len(rows) < window:
        return None  # not enough history yet to judge honestly
    values = {r["oi_strength"] for r in rows}
    if len(values) != 1:
        return None

    # Milestone 14 observability pass: a genuinely-thin-liquidity
    # commodity showing flat oi_strength isn't an anomaly -- it's the
    # honest reading of a chain with near-zero real activity (see
    # threshold_store.py's own module docstring for the investigation
    # this came from and how the symbol list is now operator-
    # configurable). Checked against the LATEST row's market_quality
    # (rows are newest-first) -- an older row's quality reading isn't
    # relevant to whether the CURRENT flatness is noise.
    latest_quality = rows[0].get("market_quality")
    suppression_symbols = config["low_liquidity_suppression_symbols"]
    if symbol in suppression_symbols and latest_quality in _LOW_LIQUIDITY_QUALITIES:
        return None

    return {
        "rule": "oi_non_responsive",
        "detail": f"{symbol} oi_strength unchanged ({values.pop()}) across last {window} logged snapshots",
    }


ALL_CHECKS = (check_bias_flip, check_confidence_unstable, check_greeks_incoherent, check_oi_non_responsive)


def evaluate_all(*, symbol: str) -> list:
    """Runs every check for one symbol, returns the list of triggered
    rule dicts (empty list if nothing triggered)."""
    return [result for check in ALL_CHECKS if (result := check(symbol=symbol)) is not None]
