from agents.trading_intelligence import trade_quality as tq
from agents.trading_intelligence import ti_store as ts
from agents.trading_intelligence.institutional_intelligence import Finding
from test_agents.trading_intelligence.conftest import insert_market_structure, insert_realistic_chain


def _closed_trade(**overrides):
    base = {
        "id": 1, "status": "CLOSED", "points": 100.0,
        "regime_trend_at_entry": None, "timeframe_alignment_score_at_entry": None,
        "institutional_backed_at_entry": None,
    }
    base.update(overrides)
    return base


class TestRegimeComponent:
    def test_trending_scores_highest(self):
        assert tq._regime_component("TRENDING") == 100.0

    def test_ranging_scores_lower_than_trending(self):
        assert tq._regime_component("RANGING") < tq._regime_component("TRENDING")

    def test_unknown_and_missing_are_excluded_not_scored(self):
        assert tq._regime_component("UNKNOWN") is None
        assert tq._regime_component(None) is None


class TestInstitutionalComponent:
    def test_backed_is_full_marks(self):
        assert tq._institutional_component(True) == 100.0

    def test_not_backed_is_zero(self):
        assert tq._institutional_component(False) == 0.0

    def test_unavailable_is_excluded_not_scored(self):
        assert tq._institutional_component(None) is None


class TestScore:
    def test_open_trade_is_not_scoreable(self, ti_db):
        result = tq.score(_closed_trade(status="OPEN", points=None))
        assert result.available is False
        assert result.score is None
        assert "not CLOSED" in result.reason

    def test_no_entry_time_context_degrades_honestly(self, ti_db):
        """A trade opened before Module 11.3 instrumentation -- no
        regime/timeframe/institutional context stored at all -- must
        never get a fabricated mid-point score."""
        result = tq.score(_closed_trade())
        assert result.available is False
        assert result.score is None
        assert "no entry-time reasoning context" in result.reason

    def test_strong_regime_alone_and_a_win_scores_highest(self, ti_db):
        result = tq.score(_closed_trade(regime_trend_at_entry="TRENDING", points=100.0))
        assert result.available is True
        assert result.setup_strength == 100.0
        assert result.outcome_alignment == 100.0  # confident (100 >= 50) AND won -> aligned
        assert result.score == 100.0

    def test_strong_regime_alone_but_a_loss_scores_the_setup_half_only(self, ti_db):
        result = tq.score(_closed_trade(regime_trend_at_entry="TRENDING", points=-50.0))
        assert result.setup_strength == 100.0
        assert result.outcome_alignment == 0.0  # confident but lost -> a "surprise"
        assert result.score == 50.0  # 100*0.5 + 0*0.5

    def test_weak_setup_that_correctly_lost_still_scores_the_outcome_half(self, ti_db):
        result = tq.score(_closed_trade(institutional_backed_at_entry=False, points=-50.0))
        assert result.setup_strength == 0.0  # not confident
        assert result.outcome_alignment == 100.0  # not confident AND lost -> aligned (honest self-assessment)
        assert result.score == 50.0  # 0*0.5 + 100*0.5

    def test_weak_setup_that_won_anyway_scores_lowest(self, ti_db):
        result = tq.score(_closed_trade(institutional_backed_at_entry=False, points=50.0))
        assert result.setup_strength == 0.0
        assert result.outcome_alignment == 0.0  # not confident but won anyway -- a lucky "surprise"
        assert result.score == 0.0

    def test_multiple_components_are_averaged(self, ti_db):
        result = tq.score(_closed_trade(
            regime_trend_at_entry="RANGING",  # 40.0
            institutional_backed_at_entry=False,  # 0.0
            points=-50.0,
        ))
        assert result.setup_strength == 20.0  # (40 + 0) / 2
        assert result.components == {"regime": 40.0, "timeframe": None, "institutional": 0.0}

    def test_zero_points_is_a_loss_not_a_win(self, ti_db):
        result = tq.score(_closed_trade(regime_trend_at_entry="TRENDING", points=0.0))
        assert result.outcome_alignment == 0.0  # confident but did NOT win -> a surprise

    def test_timeframe_alignment_score_passes_through_untouched(self, ti_db):
        result = tq.score(_closed_trade(timeframe_alignment_score_at_entry=66.7, points=10.0))
        assert result.components["timeframe"] == 66.7
        assert result.setup_strength == 66.7


class TestQualityTier:
    def test_low_tier(self):
        assert tq.quality_tier(0.0) == "LOW"
        assert tq.quality_tier(39.0) == "LOW"

    def test_medium_tier(self):
        assert tq.quality_tier(40.0) == "MEDIUM"
        assert tq.quality_tier(69.0) == "MEDIUM"

    def test_high_tier(self):
        assert tq.quality_tier(70.0) == "HIGH"
        assert tq.quality_tier(100.0) == "HIGH"

    def test_none_is_unknown(self):
        assert tq.quality_tier(None) == "UNKNOWN"


class TestInstitutionalBacking:
    def test_agreeing_finding_at_the_same_strike_is_backing(self, ti_db):
        findings = [Finding(pattern_type="InstitutionalBuying", strike=24500,
                             description="x", evidence={"side": "CE", "oi_ratio": 3.0, "signal": "Long Buildup"})]
        assert tq.institutional_backing("NIFTY", direction="CE", strike=24500, findings=findings) is True

    def test_finding_on_the_wrong_side_is_not_backing(self, ti_db):
        findings = [Finding(pattern_type="InstitutionalSelling", strike=24500,
                             description="x", evidence={"side": "PE", "oi_ratio": 3.0, "signal": "Short Buildup"})]
        assert tq.institutional_backing("NIFTY", direction="CE", strike=24500, findings=findings) is False

    def test_finding_at_a_different_strike_is_not_backing(self, ti_db):
        findings = [Finding(pattern_type="InstitutionalBuying", strike=24600,
                             description="x", evidence={"side": "CE", "oi_ratio": 3.0, "signal": "Long Buildup"})]
        assert tq.institutional_backing("NIFTY", direction="CE", strike=24500, findings=findings) is False

    def test_non_institutional_finding_is_not_backing(self, ti_db):
        findings = [Finding(pattern_type="OIWall", strike=24500, description="x", evidence={"side": "CE"})]
        assert tq.institutional_backing("NIFTY", direction="CE", strike=24500, findings=findings) is False

    def test_no_findings_at_all_is_not_backing_but_not_unavailable_either(self, ti_db):
        assert tq.institutional_backing("NIFTY", direction="CE", strike=24500, findings=[]) is False

    def test_unavailable_data_degrades_to_none_not_false(self, ti_db):
        """No cycle logged at all for this symbol -- institutional_intelligence
        itself is unavailable, a genuinely different state from "checked
        and found nothing," and must not be collapsed into False."""
        assert tq.institutional_backing("NOT_A_REAL_SYMBOL", direction="CE", strike=24500) is None

    def test_fetches_live_when_findings_not_prefetched(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        # A real chain with only Neutral signals -- no institutional flow findings.
        result = tq.institutional_backing("NIFTY", direction="CE", strike=24500)
        assert result is False


class TestScoreLifecycle:
    """Milestone 11 Plan's own success criterion for this module: 'Trade
    Quality Score computed for 100% of closed trades in a lifecycle
    test' -- opens and closes a real mix of trades (some with entry
    context, some without, wins and losses) and confirms score() runs
    for every single one without raising, honestly distinguishing
    available from unavailable rather than crashing or guessing."""

    def test_scored_for_every_closed_trade_regardless_of_available_context(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)

        for i in range(10):
            has_context = i % 2 == 0
            tid = ts.open_trade(
                symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                target_price=130.0, sl_price=85.0, qty=50,
                regime_trend_at_entry="TRENDING" if has_context else None,
                timeframe_alignment_score_at_entry=75.0 if has_context else None,
                institutional_backed_at_entry=True if has_context else None,
            )
            ts.close_trade(tid, exit_price=130.0 if i < 6 else 85.0,
                            exit_reason="TARGET HIT" if i < 6 else "STOP LOSS")

        closed = ts.list_closed_trades(symbol="NIFTY", limit=100)
        assert len(closed) == 10
        for trade in closed:
            result = tq.score(trade)
            had_context = trade.get("regime_trend_at_entry") is not None
            assert result.available is had_context
            if had_context:
                assert result.score is not None
            else:
                assert result.score is None
