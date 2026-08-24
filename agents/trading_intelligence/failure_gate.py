"""
failure_gate.py -- a structured, independent failure-first veto layer for
oi_engine.generate_signal()'s recommendations.

WHY THIS EXISTS: ARCHITECTURE_AUDIT.md (2026-08-24, Phase 1 repository
audit) found no structured failure-check gate anywhere in this repo.
generate_signal()'s confidence score is one additive point pile --
CONFIDENCE_FACTOR_ISOLATION_REPORT.md then proved its single most
influential component (PCR extremity, firing on 78% of real backtested
trades) is associated with WORSE outcomes, meaning a real red flag can
currently be numerically outweighed by unrelated bonuses rather than
independently blocking a trade. This module runs a small set of
INDEPENDENT PASS/FAIL checks that cannot be outweighed by anything else --
if any evaluated check fails, the report's overall status is BLOCKED,
full stop, regardless of how high the confidence score is.

SCOPE (deliberately narrow for this first pass): four checks, chosen
because each is either (a) genuinely new and simple enough to justify on
its own terms, or (b) a wholesale reuse of an existing, already-tested
function rather than a reimplementation:

  - reward_risk        NEW. target:SL ratio must be >= MIN_RISK_REWARD (a
                        neutral 1.0 floor -- risking no more than the
                        potential reward -- not a data-fitted number).
  - confidence          NEW as an explicit, named check, but mirrors the
                        threshold generate_signal() already gates
                        `tradeable` on -- exposes it instead of leaving
                        it implicit.
  - regime              REUSES regime_profile.classify_market_regime()
                        wholesale (chop/trend/breakout read, MCX/NSE
                        expiry-day aware) rather than reimplementing any
                        regime logic here.
  - major_level_proximity  REUSES market_structure.nearest_major_level()
                        wholesale, with the SAME ATR-multiple threshold
                        generate_signal() already uses for its own
                        structural-proximity bonus (STRUCTURAL_PROXIMITY_ATR_MULT),
                        so the two don't silently drift apart. KNOWN
                        LIMITATION: nearest_major_level() finds the single
                        closest level regardless of side -- if a hostile
                        level (blocking the trade's direction) is
                        slightly farther away than an even-nearer
                        friendly one (already behind the trade), this
                        check can miss it. A genuinely direction-aware
                        search would need extending market_structure.py
                        itself; out of scope for this pass, documented
                        here rather than silently assumed correct.

DELIBERATELY NOT INCLUDED in this pass: an OI-conflict check (dual-source
Angel/NSE disagreement) and a momentum-conflict check both exist as
*confidence adjustments* inside generate_signal() already, but neither is
exposed as a discrete, independently-callable function -- pulling them out
cleanly would mean either reimplementing their logic here (risking drift
from the real thing) or first refactoring generate_signal() to expose them
as separate return fields (a good, small, separately-justified follow-up,
not bundled into this pass). A weak-volume check was considered and
dropped for the same reason CROWDING_SCORE was flagged in the original
brief: "do NOT invent a score merely to satisfy this specification" -- no
data-driven volume floor has been established for this repo yet.

STATUS SEMANTICS: a check that couldn't be evaluated (missing
market_structure, etc.) is NOT_EVALUATED, never silently treated as PASS
or FAIL -- matches this codebase's established "degrade honestly, never
fabricate" contract (same one snapshot.available, regime_profile.classify(),
and _calibrated_probability() all already hold to). Overall FAILURE_STATUS
is BLOCKED if any EVALUATED check is FAIL; CLEAR otherwise (including when
every check is NOT_EVALUATED -- an all-unavailable report is not itself a
reason to block, just an honestly incomplete one).

SHADOW ONLY (config.TI_ENABLE_FAILURE_GATE_SHADOW, default OFF): nothing
in this repo calls run_failure_checks() from a place that can veto a real
trade. ai_trading_engine.evaluate() attaches its FailureReport to the
Recommendation purely for observation/logging when the flag is on -- same
convention as TI_ENABLE_REGIME_FILTER_SHADOW. Turning this into a real
NO_TRADE override is a decision for AFTER this gate has real backtest
evidence behind it (see FAILURE_GATE_REPORT.md), matching this project's
own established discipline for every prior signal-affecting change.
"""
import dataclasses
import datetime as dt

from market_structure import nearest_major_level
from . import regime_profile

MIN_RISK_REWARD = 1.0
STRUCTURAL_PROXIMITY_ATR_MULT = 0.5   # same constant oi_engine.generate_signal() uses for its own bonus

PASS = "PASS"
FAIL = "FAIL"
NOT_EVALUATED = "NOT_EVALUATED"

STATUS_CLEAR = "CLEAR"
STATUS_BLOCKED = "BLOCKED"


@dataclasses.dataclass
class FailureCheck:
    name: str
    status: str   # PASS | FAIL | NOT_EVALUATED
    detail: str


@dataclasses.dataclass
class FailureReport:
    status: str            # STATUS_CLEAR | STATUS_BLOCKED
    checks: list            # list[FailureCheck], every check that ran, in a fixed order
    failed: list             # names of the FAILed checks only, [] when CLEAR


def check_reward_risk(*, entry_price, sl_price, target_price, min_rr: float = MIN_RISK_REWARD) -> FailureCheck:
    if entry_price is None or sl_price is None or target_price is None:
        return FailureCheck("reward_risk", NOT_EVALUATED, "entry/SL/target not all available")
    risk = entry_price - sl_price
    reward = target_price - entry_price
    if risk <= 0:
        # Should never happen -- oi_engine.generate_signal()'s own SL formula
        # guarantees sl_price < entry_price (verified across 2,405 real
        # backtested trades in ENTRY_SL_TARGET_BACKTEST_REPORT.md). Fail
        # closed rather than divide by zero if that invariant is ever broken.
        return FailureCheck("reward_risk", FAIL, f"non-positive risk (entry={entry_price}, sl={sl_price})")
    rr = round(reward / risk, 2)
    if rr < min_rr:
        return FailureCheck("reward_risk", FAIL, f"reward:risk {rr} below floor {min_rr}")
    return FailureCheck("reward_risk", PASS, f"reward:risk {rr}")


def check_confidence(*, confidence, tradeable) -> FailureCheck:
    if confidence is None or tradeable is None:
        return FailureCheck("confidence", NOT_EVALUATED, "confidence/tradeable not available")
    if not tradeable:
        return FailureCheck("confidence", FAIL, f"confidence {confidence} below the tradeable threshold")
    return FailureCheck("confidence", PASS, f"confidence {confidence} clears the tradeable threshold")


def check_regime(*, symbol, direction, confidence, rows, atm, underlying, support, resistance,
                  market_structure, snapshot=None, expiry_date=None, is_mcx: bool = False) -> FailureCheck:
    if not market_structure or underlying is None:
        return FailureCheck("regime", NOT_EVALUATED, "no market_structure/underlying available this cycle")
    assessment = regime_profile.classify_market_regime(
        symbol, direction=direction, confidence=confidence, rows=rows, atm=atm, underlying=underlying,
        support=support, resistance=resistance, market_structure=market_structure, snapshot=snapshot,
        expiry_date=expiry_date, is_mcx=is_mcx,
    )
    wanted = regime_profile.TRADEABILITY_CE_CANDIDATE if direction == "CE" else regime_profile.TRADEABILITY_PE_CANDIDATE
    if assessment.tradeability != wanted:
        return FailureCheck("regime", FAIL, assessment.reason)
    return FailureCheck("regime", PASS, assessment.reason)


def check_major_level_proximity(*, direction, underlying, market_structure,
                                 atr_mult: float = STRUCTURAL_PROXIMITY_ATR_MULT) -> FailureCheck:
    if not market_structure or underlying is None:
        return FailureCheck("major_level_proximity", NOT_EVALUATED, "no market_structure/underlying available this cycle")
    level = nearest_major_level(underlying, market_structure)
    atr = market_structure.get("atr_14")
    if not level or atr is None or atr <= 0:
        return FailureCheck("major_level_proximity", NOT_EVALUATED, "no real structural level or ATR available yet")
    hostile_side = (level["price"] > underlying) if direction == "CE" else (level["price"] < underlying)
    max_dist = atr * atr_mult
    if hostile_side and level["distance"] <= max_dist:
        return FailureCheck(
            "major_level_proximity", FAIL,
            f"{level['name']} at {level['price']} is only {level['distance']} pts away (ATR-scaled floor {round(max_dist, 2)}), "
            f"directly in the way of this {direction} trade",
        )
    return FailureCheck(
        "major_level_proximity", PASS,
        f"nearest level {level['name']} at {level['price']} ({level['distance']} pts away) is not a hostile blocker",
    )


def run_failure_checks(*, symbol, direction, entry_price, sl_price, target_price, confidence, tradeable,
                        rows, atm, underlying=None, support=None, resistance=None, market_structure=None,
                        snapshot=None, expiry_date: dt.date | None = None, is_mcx: bool = False,
                        min_rr: float = MIN_RISK_REWARD) -> FailureReport:
    """Runs every check above, in a fixed order, and combines them into one
    FailureReport. Never raises -- a broken/unavailable input degrades that
    ONE check to NOT_EVALUATED (matching every check function's own
    contract above), it never aborts the whole report."""
    checks = [
        check_reward_risk(entry_price=entry_price, sl_price=sl_price, target_price=target_price, min_rr=min_rr),
        check_confidence(confidence=confidence, tradeable=tradeable),
        check_regime(
            symbol=symbol, direction=direction, confidence=confidence, rows=rows, atm=atm, underlying=underlying,
            support=support or [], resistance=resistance or [], market_structure=market_structure,
            snapshot=snapshot, expiry_date=expiry_date, is_mcx=is_mcx,
        ),
        check_major_level_proximity(direction=direction, underlying=underlying, market_structure=market_structure),
    ]
    failed = [c.name for c in checks if c.status == FAIL]
    status = STATUS_BLOCKED if failed else STATUS_CLEAR
    return FailureReport(status=status, checks=checks, failed=failed)
