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
"""
import statistics

from agents.intelligence_history import store as history_store
from . import threshold_store

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
    rows = history_store.list_recent(symbol=symbol, limit=2)
    if len(rows) < 2:
        return None
    latest, prev = rows[0], rows[1]
    if latest["bias"] != prev["bias"]:
        return {
            "rule": "bias_flip",
            "detail": f"{symbol} bias changed: {prev['bias']} -> {latest['bias']}",
        }
    return None


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
