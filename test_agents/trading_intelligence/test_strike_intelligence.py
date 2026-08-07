import datetime as dt

from agents.trading_intelligence import strike_intelligence as si
from oi_engine import StrikeRow


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
    def test_empty_rows_returns_empty_table(self):
        assert si.build_table([]) == []

    def test_one_row_per_strike(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        assert len(table) == 5
        assert [t.strike for t in table] == [24400, 24450, 24500, 24550, 24600]

    def test_max_pain_flag_set_on_exactly_one_strike(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        assert sum(1 for t in table if t.is_max_pain) == 1

    def test_probabilities_are_never_0_or_100(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        for t in table:
            assert 10 <= t.ai_buy_probability_pct <= 90
            assert 10 <= t.ai_sell_probability_pct <= 90

    def test_bullish_strike_has_higher_buy_than_sell_probability(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        atm = next(t for t in table if t.strike == 24500)
        assert atm.net_lean == "BULLISH"
        assert atm.ai_buy_probability_pct > atm.ai_sell_probability_pct

    def test_expected_move_computed_with_underlying_and_expiry(self):
        table = si.build_table(_realistic_rows(), underlying=24505, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert all(t.expected_move_pts is not None and t.expected_move_pts > 0 for t in table)

    def test_expected_move_none_without_expiry_date(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        assert all(t.expected_move_pts is None for t in table)

    def test_strength_scores_bounded_0_to_100(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        for t in table:
            assert 0 <= t.ce_strength <= 100
            assert 0 <= t.pe_strength <= 100

    def test_high_oi_share_strike_has_higher_strength_than_low_share(self):
        table = si.build_table(_realistic_rows(), underlying=24505)
        heavy = next(t for t in table if t.strike == 24600)  # 200000 CE OI, largest share
        light = next(t for t in table if t.strike == 24400)  # 30000 CE OI, smallest share
        assert heavy.ce_strength > light.ce_strength
