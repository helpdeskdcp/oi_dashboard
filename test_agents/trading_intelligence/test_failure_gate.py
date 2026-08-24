"""Regression tests for failure_gate.py -- the structured, independent
failure-first veto layer added post-Phase-1-audit (see
ARCHITECTURE_AUDIT.md, CONFIDENCE_FACTOR_ISOLATION_REPORT.md). Pure
function tests, no DB fixtures needed except where check_regime()
delegates to regime_profile.classify_market_regime() (same monkeypatch
seams test_regime_profile.py already establishes)."""
import datetime as dt

from oi_engine import StrikeRow

from agents.trading_intelligence import failure_gate as fg
from agents.trading_intelligence import regime_profile as rp


def _fake_regime(*, trend_regime="TRENDING", adx=30.0):
    return rp.RegimeProfile(
        symbol="TEST", trend_regime=trend_regime, adx=adx, volatility_regime="NORMAL",
        volatility_percentile=50.0, atm_strike=100, ce_buildup_persistent=False,
        pe_buildup_persistent=False, ce_persistence_cycles=0, pe_persistence_cycles=0,
    )


def _rows(strike=100):
    return [StrikeRow(strike=strike, ce_signal="Fresh Call Writing", pe_signal="Neutral")]


class TestCheckRewardRisk:
    def test_pass_when_reward_equals_risk(self):
        c = fg.check_reward_risk(entry_price=100.0, sl_price=90.0, target_price=110.0)
        assert c.status == fg.PASS

    def test_pass_when_reward_exceeds_risk(self):
        c = fg.check_reward_risk(entry_price=100.0, sl_price=95.0, target_price=120.0)
        assert c.status == fg.PASS

    def test_fail_when_reward_below_risk(self):
        c = fg.check_reward_risk(entry_price=100.0, sl_price=80.0, target_price=105.0)
        assert c.status == fg.FAIL
        assert "reward:risk" in c.detail

    def test_not_evaluated_when_missing_a_price(self):
        assert fg.check_reward_risk(entry_price=None, sl_price=90.0, target_price=110.0).status == fg.NOT_EVALUATED
        assert fg.check_reward_risk(entry_price=100.0, sl_price=None, target_price=110.0).status == fg.NOT_EVALUATED
        assert fg.check_reward_risk(entry_price=100.0, sl_price=90.0, target_price=None).status == fg.NOT_EVALUATED

    def test_fails_closed_rather_than_dividing_by_zero_on_non_positive_risk(self):
        # sl_price >= entry_price should never happen per oi_engine's own
        # invariant (verified across 2,405 real trades), but this must not
        # raise a ZeroDivisionError if it ever does.
        c = fg.check_reward_risk(entry_price=100.0, sl_price=100.0, target_price=110.0)
        assert c.status == fg.FAIL

    def test_custom_floor_is_honored(self):
        c = fg.check_reward_risk(entry_price=100.0, sl_price=95.0, target_price=108.0, min_rr=2.0)
        assert c.status == fg.FAIL   # rr=1.6 < 2.0
        c2 = fg.check_reward_risk(entry_price=100.0, sl_price=95.0, target_price=112.0, min_rr=2.0)
        assert c2.status == fg.PASS   # rr=2.4 >= 2.0


class TestCheckConfidence:
    def test_pass_when_tradeable(self):
        assert fg.check_confidence(confidence=75, tradeable=True).status == fg.PASS

    def test_fail_when_not_tradeable(self):
        assert fg.check_confidence(confidence=45, tradeable=False).status == fg.FAIL

    def test_not_evaluated_when_missing(self):
        assert fg.check_confidence(confidence=None, tradeable=None).status == fg.NOT_EVALUATED


class TestCheckRegime:
    def test_pass_when_regime_matches_ce_candidate(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=15.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        c = fg.check_regime(
            symbol="NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=104.9,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)],
            market_structure={"atr_14": 2.0}, expiry_date=dt.datetime.now().date(), is_mcx=False,
        )
        assert c.status == fg.PASS

    def test_fail_when_regime_says_no_trade(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=14.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Premium is not genuinely rising"]))
        c = fg.check_regime(
            symbol="NIFTY", direction="PE", confidence=55, rows=_rows(), atm=100, underlying=99.5,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)],
            market_structure={"atr_14": 2.0}, expiry_date=dt.datetime.now().date(), is_mcx=False,
        )
        assert c.status == fg.FAIL

    def test_not_evaluated_without_market_structure(self):
        c = fg.check_regime(
            symbol="NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=104.9,
            support=[], resistance=[], market_structure=None,
        )
        assert c.status == fg.NOT_EVALUATED

    def test_not_evaluated_without_underlying(self):
        c = fg.check_regime(
            symbol="NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=None,
            support=[], resistance=[], market_structure={"atr_14": 2.0},
        )
        assert c.status == fg.NOT_EVALUATED


class TestCheckMajorLevelProximity:
    def test_pass_when_no_market_structure(self):
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure=None)
        assert c.status == fg.NOT_EVALUATED

    def test_not_evaluated_when_no_level_found(self):
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure={"atr_14": 2.0})
        assert c.status == fg.NOT_EVALUATED

    def test_not_evaluated_when_no_atr(self):
        ms = {"prev_day": {"pdh": 101.0, "pdl": 98.0, "pdc": 99.5}}
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure=ms)
        assert c.status == fg.NOT_EVALUATED

    def test_fail_when_hostile_resistance_is_close_for_a_ce(self):
        # underlying=100, PDH=100.4 (resistance, ahead of a CE) is only 0.4
        # away, well inside 0.5*ATR=1.0 -- should FAIL.
        ms = {"prev_day": {"pdh": 100.4, "pdl": 90.0, "pdc": 95.0}, "atr_14": 2.0}
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure=ms)
        assert c.status == fg.FAIL
        assert "PDH" in c.detail

    def test_pass_when_hostile_resistance_is_far_for_a_ce(self):
        ms = {"prev_day": {"pdh": 150.0, "pdl": 90.0, "pdc": 95.0}, "atr_14": 2.0}
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure=ms)
        assert c.status == fg.PASS

    def test_pass_when_nearest_level_is_on_the_friendly_side_for_a_ce(self):
        # PDL at 99.8 is close (0.2 away) but BEHIND a CE trade (support,
        # not resistance) -- not a blocker, must PASS even though it's near.
        ms = {"prev_day": {"pdh": 200.0, "pdl": 99.8, "pdc": 150.0}, "atr_14": 2.0}
        c = fg.check_major_level_proximity(direction="CE", underlying=100.0, market_structure=ms)
        assert c.status == fg.PASS

    def test_fail_when_hostile_support_is_close_for_a_pe(self):
        ms = {"prev_day": {"pdh": 150.0, "pdl": 99.6, "pdc": 120.0}, "atr_14": 2.0}
        c = fg.check_major_level_proximity(direction="PE", underlying=100.0, market_structure=ms)
        assert c.status == fg.FAIL


class TestRunFailureChecks:
    """Aggregate behavior: status is BLOCKED if ANY evaluated check fails,
    CLEAR otherwise -- including when every check is NOT_EVALUATED (an
    honestly-incomplete report is not itself a block)."""

    def test_clear_when_minimal_inputs_all_degrade_to_not_evaluated(self):
        report = fg.run_failure_checks(
            symbol="NIFTY", direction="CE", entry_price=20.0, sl_price=15.0, target_price=30.0,
            confidence=70, tradeable=True, rows=_rows(), atm=100,
        )
        assert report.status == fg.STATUS_CLEAR
        assert report.failed == []
        assert len(report.checks) == 4

    def test_blocked_when_reward_risk_fails(self):
        report = fg.run_failure_checks(
            symbol="NIFTY", direction="CE", entry_price=20.0, sl_price=15.0, target_price=21.0,
            confidence=70, tradeable=True, rows=_rows(), atm=100,
        )
        assert report.status == fg.STATUS_BLOCKED
        assert "reward_risk" in report.failed

    def test_blocked_when_confidence_fails(self):
        report = fg.run_failure_checks(
            symbol="NIFTY", direction="CE", entry_price=20.0, sl_price=15.0, target_price=30.0,
            confidence=45, tradeable=False, rows=_rows(), atm=100,
        )
        assert report.status == fg.STATUS_BLOCKED
        assert "confidence" in report.failed

    def test_blocked_when_major_level_proximity_fails(self, monkeypatch):
        # market_structure is supplied here (needed to exercise
        # major_level_proximity), which also makes check_regime() eligible
        # to run -- monkeypatch it to a real CE_CANDIDATE PASS so this test
        # isolates the major_level_proximity failure specifically, rather
        # than hitting a real (absent, in this unit-test context) DB.
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="TRENDING", adx=30.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        ms = {"prev_day": {"pdh": 100.4, "pdl": 90.0, "pdc": 95.0}, "atr_14": 2.0}
        report = fg.run_failure_checks(
            symbol="NIFTY", direction="CE", entry_price=20.0, sl_price=15.0, target_price=30.0,
            confidence=70, tradeable=True, rows=_rows(), atm=100, underlying=100.0, market_structure=ms,
        )
        assert report.status == fg.STATUS_BLOCKED
        assert "major_level_proximity" in report.failed

    def test_never_raises_on_completely_empty_optional_inputs(self):
        report = fg.run_failure_checks(
            symbol="NIFTY", direction="PE", entry_price=None, sl_price=None, target_price=None,
            confidence=None, tradeable=None, rows=[], atm=None,
        )
        assert report.status == fg.STATUS_CLEAR
        assert all(c.status == fg.NOT_EVALUATED for c in report.checks)
