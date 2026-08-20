"""
test_expiry_intelligence.py -- Milestone 17+: regression tests for
expiry_intelligence.py's two halves:

1. Expiry resolution (get_expiry_status / get_all_index_expiry_flags /
   global_context_from_flags) -- exercised against a FakeFetcher (a plain
   object with a list_available_expiries(symbol) method, matching
   AngelOneFetcher's own signature) rather than app.py's real
   AngelOneFetcher, so these tests never touch a broker session, the
   network, or the real instrument-master cache file.

2. Expiry-day OI/scalping analytics (compute_scalping_metrics) --
   exercised against synthetic oi_engine.StrikeRow instances, the same
   technique oi_engine.py's own tests already use.

No test in this file imports app.py.
"""
import datetime as dt

import pytest

import expiry_intelligence as ei
from oi_engine import StrikeRow


class FakeFetcher:
    """Duck-types AngelOneFetcher.list_available_expiries(symbol) ->
    list[date] -- the only method expiry_intelligence.py's expiry-
    resolution functions ever call on a fetcher."""

    def __init__(self, expiries_by_symbol):
        self._expiries = expiries_by_symbol

    def list_available_expiries(self, symbol):
        return list(self._expiries.get(symbol, []))


TODAY = dt.date(2026, 8, 11)   # a Tuesday, deliberately not "the usual Thursday" -- see module docstring


# ---------------------------------------------------------------------------
# Expiry resolution
# ---------------------------------------------------------------------------

class TestGetExpiryStatus:
    def test_nearest_expiry_selection(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 20), dt.date(2026, 8, 13), dt.date(2026, 8, 27)]})
        status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert status["next_expiry"] == dt.date(2026, 8, 13)
        assert status["days_to_expiry"] == 2

    def test_expiry_today_flag_true(self):
        fetcher = FakeFetcher({"NIFTY": [TODAY, dt.date(2026, 8, 20)]})
        status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert status["expiry_today"] is True
        assert status["days_to_expiry"] == 0

    def test_expiry_today_flag_false(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13)]})
        status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert status["expiry_today"] is False

    def test_holiday_shifted_expiry_is_picked_up_transparently(self):
        """No fixed weekday table anywhere -- if the exchange shifts a
        weekly expiry off its usual day (a holiday), the instrument
        master simply lists a different date, and get_expiry_status
        reports THAT date with no special-casing required."""
        holiday_shifted = dt.date(2026, 8, 12)   # NOT the "usual" Thursday-ish date, on purpose
        fetcher = FakeFetcher({"NIFTY": [holiday_shifted, dt.date(2026, 8, 20)]})
        status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert status["next_expiry"] == holiday_shifted
        assert status["days_to_expiry"] == 1

    def test_empty_expiry_list_raises_controlled_exception(self):
        fetcher = FakeFetcher({})   # no NIFTY key at all
        with pytest.raises(ei.ExpiryDataUnavailable):
            ei.get_expiry_status("NIFTY", fetcher, today=TODAY)

    def test_all_past_dates_raises_rather_than_selecting_an_expired_contract(self):
        """Fixed 2026-08-20 (Codex review, HIGH): previously degraded to
        the most-recent PAST date rather than raising -- a past
        next_expiry fed a negative days_to_expiry into every downstream
        caller (Black-Scholes time-to-expiry etc.), which is nonsensical.
        Fail closed instead: every real caller already handles
        ExpiryDataUnavailable as an honest unavailable state."""
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 1), dt.date(2026, 8, 5)]})
        with pytest.raises(ei.ExpiryDataUnavailable):
            ei.get_expiry_status("NIFTY", fetcher, today=TODAY)

    def test_monthly_expiry_identification(self):
        """MONTHLY = the LAST listed expiry within its own (year, month) --
        never a fixed weekday rule. Two dates in August, the later one is
        monthly; the earlier is weekly."""
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13), dt.date(2026, 8, 20), dt.date(2026, 8, 27)]})
        weekly_status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert weekly_status["next_expiry"] == dt.date(2026, 8, 13)
        assert weekly_status["weekly_or_monthly"] == "WEEKLY"
        assert weekly_status["is_weekly"] is True
        assert weekly_status["is_monthly"] is False

        monthly_status = ei.get_expiry_status("NIFTY", fetcher, today=dt.date(2026, 8, 26))
        assert monthly_status["next_expiry"] == dt.date(2026, 8, 27)
        assert monthly_status["weekly_or_monthly"] == "MONTHLY"
        assert monthly_status["is_weekly"] is False
        assert monthly_status["is_monthly"] is True

    def test_source_and_exchange_fields(self):
        fetcher = FakeFetcher({"SENSEX": [dt.date(2026, 8, 13)]})
        status = ei.get_expiry_status("SENSEX", fetcher, today=TODAY, exchange="BSE")
        assert status["source"] == "angelone_instrument_master"
        assert status["exchange"] == "BSE"

    def test_exchange_defaults_to_none_when_not_supplied(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13)]})
        status = ei.get_expiry_status("NIFTY", fetcher, today=TODAY)
        assert status["exchange"] is None


class TestGetNearestExpiry:
    def test_returns_just_the_date(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13), dt.date(2026, 8, 20)]})
        assert ei.get_nearest_expiry("NIFTY", fetcher, today=TODAY) == dt.date(2026, 8, 13)

    def test_raises_for_unknown_symbol(self):
        fetcher = FakeFetcher({})
        with pytest.raises(ei.ExpiryDataUnavailable):
            ei.get_nearest_expiry("NOPE", fetcher, today=TODAY)


class TestLoadAvailableExpiries:
    def test_passes_through_from_fetcher(self):
        dates = [dt.date(2026, 8, 13), dt.date(2026, 8, 20)]
        fetcher = FakeFetcher({"NIFTY": dates})
        assert ei.load_available_expiries("NIFTY", fetcher) == dates

    def test_empty_for_unknown_symbol(self):
        fetcher = FakeFetcher({})
        assert ei.load_available_expiries("NOPE", fetcher) == []


class TestGetAllIndexExpiryFlags:
    def test_multiple_indexes_resolved_independently(self):
        fetcher = FakeFetcher({
            "NIFTY": [dt.date(2026, 8, 13)],
            "BANKNIFTY": [dt.date(2026, 8, 25)],
        })
        flags = ei.get_all_index_expiry_flags(fetcher, indexes=["NIFTY", "BANKNIFTY"], today=TODAY)
        assert flags["NIFTY"]["next_expiry"] == dt.date(2026, 8, 13)
        assert flags["BANKNIFTY"]["next_expiry"] == dt.date(2026, 8, 25)

    def test_one_missing_symbol_degrades_without_blanking_others(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13)]})
        flags = ei.get_all_index_expiry_flags(fetcher, indexes=["NIFTY", "MISSING"], today=TODAY)
        assert flags["NIFTY"]["next_expiry"] == dt.date(2026, 8, 13)
        assert "error" in flags["MISSING"]

    def test_indexes_as_dict_echoes_exchange(self):
        fetcher = FakeFetcher({"NIFTY": [dt.date(2026, 8, 13)], "SENSEX": [dt.date(2026, 8, 14)]})
        flags = ei.get_all_index_expiry_flags(fetcher, indexes={"NIFTY": "NSE", "SENSEX": "BSE"}, today=TODAY)
        assert flags["NIFTY"]["exchange"] == "NSE"
        assert flags["SENSEX"]["exchange"] == "BSE"

    def test_default_indexes_used_when_none_given(self):
        fetcher = FakeFetcher({name: [dt.date(2026, 8, 13)] for name in ei.DEFAULT_INDEXES})
        flags = ei.get_all_index_expiry_flags(fetcher, today=TODAY)
        assert set(flags.keys()) == set(ei.DEFAULT_INDEXES)


class TestGlobalExpiryContext:
    def test_today_and_tomorrow_lists(self):
        fetcher = FakeFetcher({
            "NIFTY": [TODAY],
            "BANKNIFTY": [TODAY + dt.timedelta(days=1)],
            "FINNIFTY": [TODAY + dt.timedelta(days=5)],
        })
        ctx = ei.get_global_expiry_context(fetcher, indexes=["NIFTY", "BANKNIFTY", "FINNIFTY"], today=TODAY)
        assert ctx["today_expiry_indexes"] == ["NIFTY"]
        assert ctx["tomorrow_expiry_indexes"] == ["BANKNIFTY"]
        assert ctx["high_gamma_day"] is True

    def test_high_gamma_day_false_when_nothing_expires_today(self):
        fetcher = FakeFetcher({"NIFTY": [TODAY + dt.timedelta(days=3)]})
        ctx = ei.get_global_expiry_context(fetcher, indexes=["NIFTY"], today=TODAY)
        assert ctx["today_expiry_indexes"] == []
        assert ctx["high_gamma_day"] is False

    def test_monthly_expiry_week_true_within_seven_days(self):
        # Only expiry listed for NIFTY this month -> it's simultaneously
        # the nearest AND the last-of-month -> MONTHLY, 3 days out.
        fetcher = FakeFetcher({"NIFTY": [TODAY + dt.timedelta(days=3)]})
        ctx = ei.get_global_expiry_context(fetcher, indexes=["NIFTY"], today=TODAY)
        assert ctx["monthly_expiry_week"] is True

    def test_monthly_expiry_week_false_when_monthly_is_far_out(self):
        fetcher = FakeFetcher({"NIFTY": [TODAY + dt.timedelta(days=20)]})
        ctx = ei.get_global_expiry_context(fetcher, indexes=["NIFTY"], today=TODAY)
        assert ctx["monthly_expiry_week"] is False

    def test_errored_index_excluded_from_global_flags_not_crashing(self):
        fetcher = FakeFetcher({"NIFTY": [TODAY]})
        ctx = ei.get_global_expiry_context(fetcher, indexes=["NIFTY", "MISSING"], today=TODAY)
        assert ctx["today_expiry_indexes"] == ["NIFTY"]

    def test_global_context_from_flags_matches_get_global_expiry_context(self):
        fetcher = FakeFetcher({"NIFTY": [TODAY]})
        flags = ei.get_all_index_expiry_flags(fetcher, indexes=["NIFTY"], today=TODAY)
        assert ei.global_context_from_flags(flags) == ei.get_global_expiry_context(
            fetcher, indexes=["NIFTY"], today=TODAY,
        )


# ---------------------------------------------------------------------------
# Expiry-day OI/scalping analytics
# ---------------------------------------------------------------------------

def _rows(atm_ce_oi=10000, atm_ce_oi_chg=-2000, atm_pe_oi=8000, atm_pe_oi_chg=500,
          atm_ce_signal="Long Unwinding", atm_pe_signal="Neutral"):
    """24000 is ATM for underlying=24010/step=50. 24050 carries the max
    OI on both sides so oi_walls() picks it as the CE/PE wall."""
    return [
        StrikeRow(strike=24000, ce_oi=atm_ce_oi, ce_oi_chg=atm_ce_oi_chg, ce_signal=atm_ce_signal,
                  pe_oi=atm_pe_oi, pe_oi_chg=atm_pe_oi_chg, pe_signal=atm_pe_signal),
        StrikeRow(strike=24050, ce_oi=15000, ce_oi_chg=3000, pe_oi=12000, pe_oi_chg=-500),
    ]


class TestComputeScalpingMetrics:
    def test_none_when_no_rows(self):
        assert ei.compute_scalping_metrics([], underlying=24010, step=50, days_to_expiry=0) is None

    def test_none_when_no_underlying(self):
        assert ei.compute_scalping_metrics(_rows(), underlying=None, step=50, days_to_expiry=0) is None

    def test_none_when_atm_unresolvable(self):
        # No step given, no atm given, and rows() only has 2 strikes 50 apart --
        # step IS inferable here (50), so instead force unresolvability by
        # passing a single-strike chain (no gap to infer a step from).
        one_row = [StrikeRow(strike=24000, ce_oi=100, pe_oi=100)]
        assert ei.compute_scalping_metrics(one_row, underlying=24010, days_to_expiry=0) is None

    def test_atm_strike_and_oi_walls(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, step=50, days_to_expiry=5)
        assert metrics["atm_strike"] == 24000
        assert metrics["max_call_oi_strike"] == 24050
        assert metrics["max_put_oi_strike"] == 24050
        assert metrics["max_call_oi_change"] == 3000
        assert metrics["max_put_oi_change"] == -500

    def test_call_unwinding_detected(self):
        metrics = ei.compute_scalping_metrics(
            _rows(atm_ce_signal="Long Unwinding", atm_pe_signal="Neutral"),
            underlying=24010, step=50, days_to_expiry=5,
        )
        assert metrics["call_unwinding_detected"] is True
        assert metrics["put_unwinding_detected"] is False

    def test_put_unwinding_detected(self):
        metrics = ei.compute_scalping_metrics(
            _rows(atm_ce_signal="Neutral", atm_pe_signal="Long Unwinding"),
            underlying=24010, step=50, days_to_expiry=5,
        )
        assert metrics["put_unwinding_detected"] is True
        assert metrics["call_unwinding_detected"] is False

    def test_gamma_risk_zone_true_within_one_step(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, step=50, days_to_expiry=5)
        assert metrics["gamma_risk_zone"] is True   # |24010 - 24000| = 10 <= 50

    def test_gamma_risk_zone_false_far_from_atm(self):
        # find_atm() always rounds to the NEAREST strike, so gamma_risk_zone
        # is structurally always True when atm is auto-derived from
        # underlying/step (distance is at most step/2). Isolate the "far"
        # case with an explicit atm override instead.
        far_metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, atm=25000, step=50, days_to_expiry=5)
        assert far_metrics["gamma_risk_zone"] is False

    def test_theta_decay_mode_on_expiry_day(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, step=50, days_to_expiry=0)
        assert metrics["theta_decay_mode"] is True
        assert metrics["expiry_day_trade_params"] == ei.EXPIRY_DAY_TRADE_PARAMS

    def test_theta_decay_mode_off_when_not_expiry_day(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, step=50, days_to_expiry=3)
        assert metrics["theta_decay_mode"] is False
        assert "expiry_day_trade_params" not in metrics

    def test_atm_override_skips_find_atm(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, atm=24050, days_to_expiry=0)
        assert metrics["atm_strike"] == 24050

    def test_step_inferred_from_rows_when_not_given(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, days_to_expiry=0)
        assert metrics["atm_strike"] == 24000   # find_atm(24010, step=50) == 24000, inferred from strike gap

    def test_expiry_pressure_score_within_documented_bounds(self):
        metrics = ei.compute_scalping_metrics(_rows(), underlying=24010, step=50, days_to_expiry=0)
        assert 0 <= metrics["expiry_pressure_score"] <= 13

    def test_expiry_pressure_score_higher_with_both_sides_unwinding_and_expiry_day(self):
        calm = ei.compute_scalping_metrics(
            _rows(atm_ce_signal="Neutral", atm_pe_signal="Neutral", atm_ce_oi_chg=0, atm_pe_oi_chg=0),
            underlying=24010, step=50, days_to_expiry=10,
        )
        stormy = ei.compute_scalping_metrics(
            _rows(atm_ce_signal="Long Unwinding", atm_pe_signal="Long Unwinding"),
            underlying=24010, step=50, days_to_expiry=0,
        )
        assert stormy["expiry_pressure_score"] > calm["expiry_pressure_score"]

    def test_avg_atm_volume_not_provided_scores_zero_spike(self):
        # No avg_atm_volume given -- _atm_volume_spike must contribute 0,
        # never a fabricated spike score. atm=24000 (a real row, so
        # atm_row still resolves) with underlying far away disables the
        # gamma_risk_zone contribution so pressure score isolates volume.
        rows = _rows(atm_ce_signal="Neutral", atm_pe_signal="Neutral", atm_ce_oi_chg=0, atm_pe_oi_chg=0)
        baseline = ei.compute_scalping_metrics(rows, underlying=30000, atm=24000, step=50, days_to_expiry=10)
        assert baseline["expiry_pressure_score"] == 0

    def test_avg_atm_volume_spike_raises_pressure_score(self):
        rows = _rows(atm_ce_signal="Neutral", atm_pe_signal="Neutral", atm_ce_oi_chg=0, atm_pe_oi_chg=0)
        rows[0].ce_vol, rows[0].pe_vol = 3000, 3000
        no_baseline = ei.compute_scalping_metrics(rows, underlying=30000, atm=24000, step=50, days_to_expiry=10)
        with_baseline = ei.compute_scalping_metrics(
            rows, underlying=30000, atm=24000, step=50, days_to_expiry=10, avg_atm_volume=1000,
        )
        assert no_baseline["expiry_pressure_score"] == 0
        assert with_baseline["expiry_pressure_score"] > 0
