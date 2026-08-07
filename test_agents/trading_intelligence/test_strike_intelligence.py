import datetime as dt

from oi_engine import StrikeRow

from agents.trading_intelligence import strike_intelligence as si
from test_agents.trading_intelligence.conftest import insert_cycle, insert_strike


def _realistic_rows():
    return [
        StrikeRow(strike=24400, ce_oi=30000, ce_signal="Neutral", ce_iv=15.0, ce_ltp=180.0,
                  pe_oi=95000, pe_signal="Long Buildup", pe_iv=15.5, pe_ltp=40.0),
        StrikeRow(strike=24450, ce_oi=60000, ce_signal="Neutral", ce_iv=15.2, ce_ltp=150.0,
                  pe_oi=70000, pe_signal="Neutral", pe_iv=15.2, pe_ltp=55.0),
        StrikeRow(strike=24500, ce_oi=105000, ce_signal="Long Buildup", ce_iv=16.0, ce_ltp=135.0,
                  pe_oi=88000, pe_signal="Long Unwinding", pe_iv=15.0, pe_ltp=90.0),
        StrikeRow(strike=24550, ce_oi=70000, ce_signal="Neutral", ce_iv=16.2, ce_ltp=90.0,
                  pe_oi=50000, pe_signal="Neutral", pe_iv=16.2, pe_ltp=110.0),
        StrikeRow(strike=24600, ce_oi=200000, ce_signal="Neutral", ce_iv=16.5, ce_ltp=50.0,
                  pe_oi=40000, pe_signal="Neutral", pe_iv=16.5, pe_ltp=140.0),
    ]


class TestBuildTable:
    def test_empty_rows_returns_empty_table(self, ti_db):
        assert si.build_table("NIFTY", []) == []

    def test_one_row_per_strike(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert len(table) == 5
        assert [t.strike for t in table] == [24400, 24450, 24500, 24550, 24600]

    def test_max_pain_flag_set_on_exactly_one_strike(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert sum(1 for t in table if t.is_max_pain) == 1

    def test_max_pain_distance_is_zero_at_max_pain_strike(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        max_pain_row = next(t for t in table if t.is_max_pain)
        assert max_pain_row.max_pain_distance == 0.0
        other = next(t for t in table if not t.is_max_pain)
        assert other.max_pain_distance > 0.0

    def test_probabilities_are_never_0_or_100(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        for t in table:
            assert 10 <= t.ai_buy_probability_pct <= 90
            assert 10 <= t.ai_sell_probability_pct <= 90

    def test_bullish_strike_has_higher_buy_than_sell_probability(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        atm = next(t for t in table if t.strike == 24500)
        assert atm.net_lean == "BULLISH"
        assert atm.ai_buy_probability_pct > atm.ai_sell_probability_pct

    def test_expected_move_computed_with_underlying_and_expiry(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505,
                                expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert all(t.expected_move_pts is not None and t.expected_move_pts > 0 for t in table)

    def test_expected_move_none_without_expiry_date(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert all(t.expected_move_pts is None for t in table)

    def test_support_and_resistance_strength_bounded_0_to_100(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        for t in table:
            assert 0 <= t.resistance_strength <= 100
            assert 0 <= t.support_strength <= 100

    def test_high_oi_share_strike_has_higher_resistance_strength_than_low_share(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        heavy = next(t for t in table if t.strike == 24600)  # 200000 CE OI, largest share
        light = next(t for t in table if t.strike == 24400)  # 30000 CE OI, smallest share
        assert heavy.resistance_strength > light.resistance_strength

    def test_oi_wall_score_reflects_share_of_total_chain_oi(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        total_oi = sum(r.ce_oi + r.pe_oi for r in _realistic_rows())
        heavy = next(t for t in table if t.strike == 24600)
        expected = round((200000 + 40000) / total_oi * 100, 1)
        assert heavy.oi_wall_score == expected

    def test_gamma_and_delta_exposure_use_filled_in_greeks(self, ti_db):
        """underlying+expiry_date given -> market_data.fill_missing_greeks
        runs first, so exposure fields must be genuinely non-zero, not
        left at the coerced-zero default (0 OI * 0 gamma)."""
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505,
                                expiry_date=dt.date.today() + dt.timedelta(days=2))
        atm = next(t for t in table if t.strike == 24500)
        assert atm.ce_gamma_exposure != 0.0
        assert atm.ce_delta_exposure != 0.0

    def test_no_gamma_exposure_without_expiry_date(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        atm = next(t for t in table if t.strike == 24500)
        assert atm.ce_gamma_exposure == 0.0

    def test_prob_itm_bounded_0_to_1_when_computable(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505,
                                expiry_date=dt.date.today() + dt.timedelta(days=2))
        for t in table:
            if t.ce_prob_itm is not None:
                assert 0.0 <= t.ce_prob_itm <= 1.0
            if t.pe_prob_itm is not None:
                assert 0.0 <= t.pe_prob_itm <= 1.0

    def test_prob_itm_none_without_expiry_date(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert all(t.ce_prob_itm is None for t in table)

    def test_deep_itm_call_has_higher_prob_itm_than_deep_otm_call(self, ti_db):
        rows = [
            StrikeRow(strike=23000, ce_oi=10000, ce_iv=16.0, ce_ltp=1500.0),  # deep ITM for a 24505 spot
            StrikeRow(strike=26000, ce_oi=10000, ce_iv=16.0, ce_ltp=5.0),     # deep OTM
        ]
        table = si.build_table("NIFTY", rows, underlying=24505, expiry_date=dt.date.today() + dt.timedelta(days=5))
        itm = next(t for t in table if t.strike == 23000)
        otm = next(t for t in table if t.strike == 26000)
        assert itm.ce_prob_itm > otm.ce_prob_itm

    def test_iv_rank_none_with_insufficient_history(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert all(t.ce_iv_rank is None for t in table)

    def test_iv_rank_computed_from_real_history(self, ti_db):
        # Seed 5 prior cycles with a rising CE IV at strike 24500, then the
        # current (highest) reading should rank near 100.
        for i, iv in enumerate((12.0, 13.0, 14.0, 15.0, 16.0)):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15+i}:00")
            insert_strike(ti_db, cid, 24500, ce_iv=iv)
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)  # ce_iv=16.0 at 24500
        atm = next(t for t in table if t.strike == 24500)
        assert atm.ce_iv_rank == 100.0

    def test_premium_momentum_none_with_insufficient_history(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505)
        assert all(t.ce_premium_momentum is None for t in table)

    def test_premium_momentum_positive_when_premium_rising(self, ti_db):
        for i, ltp in enumerate((90.0, 95.0, 100.0, 110.0)):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15+i}:00")
            insert_strike(ti_db, cid, 24500, ce_ltp=ltp)
        rows = _realistic_rows()  # ce_ltp=135.0 at 24500, continuing the rise
        table = si.build_table("NIFTY", rows, underlying=24505)
        atm = next(t for t in table if t.strike == 24500)
        assert atm.ce_premium_momentum > 0

    def test_ai_strike_score_bounded_0_to_100(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505,
                                expiry_date=dt.date.today() + dt.timedelta(days=2))
        for t in table:
            assert 0 <= t.ai_strike_score <= 100

    def test_atm_with_fresh_buildup_scores_higher_than_a_quiet_far_strike(self, ti_db):
        table = si.build_table("NIFTY", _realistic_rows(), underlying=24505,
                                expiry_date=dt.date.today() + dt.timedelta(days=2))
        atm = next(t for t in table if t.strike == 24500)  # Long Buildup, near-money
        far = next(t for t in table if t.strike == 24400)  # Neutral CE side
        assert atm.ai_strike_score > far.ai_strike_score
