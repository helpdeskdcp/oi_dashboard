"""
agents/trading_intelligence/risk_filters.py -- Milestone 20, Phase 6:
Smart Intraday Risk Filters.

ADVISORY / ANALYTICS ONLY -- exactly as requested: "Add non-executing
validation filters for analytics and paper trading... Do not execute
anything automatically." Nothing here opens, blocks, or resizes a real
or paper trade; it only classifies a setup as PASSED or FILTER_REJECTED,
for a caller to log/display. Not wired into ai_trading_engine.evaluate()
or paper_trading.enter_from_recommendation() -- doing so would make this
a real entry gate, which is explicitly out of scope here.

Every threshold below (CE/PE_MIN_CONFIDENCE=82, the state lists, etc.)
is a CHOSEN cutoff taken directly from the request that authorized this
module, NOT a calibrated one -- unlike trade_quality.
calibrated_regime_score() (Milestone 20, Phase 6's own regime-sizing
fix), there is no live win-rate data behind these specific numbers yet.
Documented here so a future calibration pass can replace them honestly,
the same "guessed vs derived" distinction this project treats seriously
elsewhere (see trade_quality.py's own REGIME_TREND_SCORE history).

Real gap, stated plainly: CE_ALLOWED_STATES/PE_ALLOWED_STATES include
"BULLISH_CONTINUATION"/"BEARISH_CONTINUATION" because the request that
authorized this module named them -- but institutional_levels.
classify_market_state() does not currently produce those two states
(its real output set is KNOWN_MARKET_STATES below). Final safety
adjustment: a `state` that isn't in KNOWN_MARKET_STATES at all --
including "BULLISH_CONTINUATION"/"BEARISH_CONTINUATION", since no real
classifier has ever produced them -- is rejected IMMEDIATELY with
reason "unsupported_market_state", checked BEFORE the CE/PE_ALLOWED_
STATES membership test. Without this, a "CONTINUATION" state would
have silently PASSED that later check (it IS in the allow-list string),
even though no real signal has ever actually carried that state --
exactly the "don't silently pass an unsupported state" failure mode
this adjustment exists to close. A setup in either "CONTINUATION" state
therefore still always fails this filter, but now for the right,
distinguishable reason, not by coincidentally not-yet-existing.
"""
import dataclasses

CE_MIN_CONFIDENCE = 82
PE_MIN_CONFIDENCE = 82
CE_ALLOWED_STATES = ("BULLISH_RETEST_ACTIVE", "BULLISH_CONTINUATION")
PE_ALLOWED_STATES = ("BEARISH_RETEST_ACTIVE", "BEARISH_CONTINUATION")

# The REAL, only-ever-producible output set of institutional_levels.
# classify_market_state() -- imported by name (not the whole module) to
# avoid pulling this package's broker-adjacent-safety boundary into a
# repo-root module unnecessarily; these are just plain string constants.
KNOWN_MARKET_STATES = (
    "TRENDING_UP", "TRENDING_DOWN", "RANGE", "BREAKOUT_WATCH", "BREAKDOWN_WATCH",
    "BULLISH_RETEST_ACTIVE", "BEARISH_RETEST_ACTIVE", "REVERSAL_RISK",
)
UNSUPPORTED_STATE_REASON = "unsupported_market_state"


@dataclasses.dataclass
class FilterResult:
    passed: bool
    trade_quality: str | None   # "FILTER_REJECTED" when not passed, else None
    reasons: list   # empty when passed -- every failed condition, never just the first


def evaluate_ce(*, state: str | None, confidence: int | None, spot: float | None, vwap: float | None,
                 breakout_confirmed: bool, premium_momentum_positive_2_candles: bool) -> FilterResult:
    """Allow CE only if: state is a bullish retest/continuation state,
    confidence >= CE_MIN_CONFIDENCE, spot > VWAP, the breakout candle's
    close is confirmed, and option premium momentum has been positive
    for 2 candles. Every unmet condition is reported, not just the
    first, so a caller can show/log the FULL reason a setup was
    rejected -- EXCEPT an unsupported state, which short-circuits alone
    (see UNSUPPORTED_STATE_REASON's own docstring note above)."""
    if state not in KNOWN_MARKET_STATES:
        return FilterResult(passed=False, trade_quality="FILTER_REJECTED", reasons=[UNSUPPORTED_STATE_REASON])

    reasons = []
    if state not in CE_ALLOWED_STATES:
        reasons.append(f"state {state!r} not in {CE_ALLOWED_STATES}")
    if confidence is None or confidence < CE_MIN_CONFIDENCE:
        reasons.append(f"confidence {confidence} < {CE_MIN_CONFIDENCE}")
    if spot is None or vwap is None or not (spot > vwap):
        reasons.append(f"spot ({spot}) not above VWAP ({vwap})")
    if not breakout_confirmed:
        reasons.append("breakout candle close not confirmed")
    if not premium_momentum_positive_2_candles:
        reasons.append("option premium momentum not positive for 2 candles")
    passed = not reasons
    return FilterResult(passed=passed, trade_quality=None if passed else "FILTER_REJECTED", reasons=reasons)


def evaluate_pe(*, state: str | None, confidence: int | None, spot: float | None, vwap: float | None,
                 breakdown_confirmed: bool, put_oi_increasing: bool, call_oi_unwinding: bool) -> FilterResult:
    """Allow PE only if: state is a bearish retest/continuation state,
    confidence >= PE_MIN_CONFIDENCE, spot < VWAP, the breakdown candle's
    close is confirmed, put OI is increasing, and call OI is unwinding.
    Same unsupported-state short-circuit as evaluate_ce()."""
    if state not in KNOWN_MARKET_STATES:
        return FilterResult(passed=False, trade_quality="FILTER_REJECTED", reasons=[UNSUPPORTED_STATE_REASON])

    reasons = []
    if state not in PE_ALLOWED_STATES:
        reasons.append(f"state {state!r} not in {PE_ALLOWED_STATES}")
    if confidence is None or confidence < PE_MIN_CONFIDENCE:
        reasons.append(f"confidence {confidence} < {PE_MIN_CONFIDENCE}")
    if spot is None or vwap is None or not (spot < vwap):
        reasons.append(f"spot ({spot}) not below VWAP ({vwap})")
    if not breakdown_confirmed:
        reasons.append("breakdown candle close not confirmed")
    if not put_oi_increasing:
        reasons.append("put OI not increasing")
    if not call_oi_unwinding:
        reasons.append("call OI not unwinding")
    passed = not reasons
    return FilterResult(passed=passed, trade_quality=None if passed else "FILTER_REJECTED", reasons=reasons)
