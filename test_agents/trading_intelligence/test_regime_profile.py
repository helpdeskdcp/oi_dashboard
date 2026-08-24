import datetime as dt

import pandas as pd

from oi_engine import StrikeRow

from agents.trading_intelligence import regime_profile as rp
from test_agents.trading_intelligence.conftest import (
    insert_cycle,
    insert_market_structure,
    insert_realistic_chain,
    insert_strike,
)


class TestVolatilityRegime:
    def test_unknown_below_minimum_history(self, ti_db):
        regime, pct = rp._volatility_regime(15.0, [10.0, 11.0])  # only 2 readings, need 5
        assert regime == "UNKNOWN"
        assert pct is None

    def test_unknown_when_current_atr_missing(self, ti_db):
        regime, pct = rp._volatility_regime(None, [10.0, 11.0, 12.0, 13.0, 14.0])
        assert regime == "UNKNOWN"
        assert pct is None

    def test_high_when_current_is_the_top_of_its_own_range(self, ti_db):
        regime, pct = rp._volatility_regime(30.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "HIGH"
        assert pct == 100.0

    def test_low_when_current_is_the_bottom_of_its_own_range(self, ti_db):
        regime, pct = rp._volatility_regime(5.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "LOW"
        assert pct == 0.0

    def test_normal_when_current_is_mid_range(self, ti_db):
        regime, pct = rp._volatility_regime(14.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "NORMAL"

    def test_normal_when_history_is_perfectly_flat(self, ti_db):
        regime, pct = rp._volatility_regime(10.0, [10.0, 10.0, 10.0, 10.0, 10.0])
        assert regime == "NORMAL"
        assert pct == 50.0

    def test_none_values_and_zeros_in_history_are_ignored(self, ti_db):
        # 5 usable readings required -- Nones/zeros must not count toward that.
        regime, pct = rp._volatility_regime(30.0, [10.0, None, 0.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "HIGH"


class TestPersistence:
    def test_no_history_is_not_persistent(self, ti_db):
        persistent, cycles = rp._persistence([], "ce_signal")
        assert persistent is False
        assert cycles == 0

    def test_neutral_current_signal_is_never_persistent(self, ti_db):
        history = [{"ce_signal": "Neutral"}, {"ce_signal": "Neutral"}, {"ce_signal": "Neutral"}]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is False
        assert cycles == 0

    def test_persistent_when_min_cycles_agree_consecutively(self, ti_db):
        history = [{"ce_signal": "Long Buildup"}] * 4
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is True
        assert cycles == 4

    def test_not_yet_persistent_below_min_cycles(self, ti_db):
        history = [{"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"}, {"ce_signal": "Neutral"}]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is False
        assert cycles == 2

    def test_stops_counting_at_first_disagreement(self, ti_db):
        history = [
            {"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"},
            {"ce_signal": "Short Buildup"}, {"ce_signal": "Long Buildup"},  # older, disagreeing/agreeing -- irrelevant
        ]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is True
        assert cycles == 3


class TestClassify:
    def test_unavailable_symbol_degrades_honestly(self, ti_db):
        profile = rp.classify("NOT_A_REAL_SYMBOL")
        assert profile.trend_regime == "UNKNOWN"
        assert profile.adx is None
        assert profile.volatility_regime == "UNKNOWN"
        assert profile.atm_strike is None
        assert profile.ce_buildup_persistent is False
        assert profile.pe_buildup_persistent is False

    def test_trend_regime_reflects_stored_adx(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        profile = rp.classify("NIFTY")
        assert profile.trend_regime == "TRENDING"
        assert profile.adx == 30.0

    def test_volatility_regime_computed_from_real_history(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        # 5 prior readings (need >= VOLATILITY_RANK_MIN_HISTORY=5) + 1 current outlier-high reading.
        for i, atr in enumerate((10.0, 11.0, 12.0, 14.0, 16.0, 30.0)):
            insert_market_structure(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15 + i}:00", atr_14=atr)
        profile = rp.classify("NIFTY")
        assert profile.volatility_regime == "HIGH"

    def test_atm_persistence_reflects_real_strike_history(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        # insert_realistic_chain's own cycle is the LATEST (current) one -- its
        # ATM strike defaults to ce_signal="Neutral"; override it, then add
        # 3 MORE, older cycles with the same non-neutral signal before it.
        import sqlite3
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_signal='Long Buildup' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        for i in range(3):
            older_cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{10 + i}:00",
                                      underlying_ltp=24505.0, atm=24500.0)
            insert_strike(ti_db, older_cid, 24500, ce_signal="Long Buildup")

        profile = rp.classify("NIFTY")
        assert profile.atm_strike == 24500
        assert profile.ce_buildup_persistent is True
        assert profile.ce_persistence_cycles == 4  # the current cycle + 3 older ones

    def test_snapshot_and_market_structure_can_be_prefetched(self, ti_db):
        """Dedup discipline: a caller that already has both (the way
        ai_trading_engine.evaluate() will, once wired in) must be able to
        pass them straight through without a second read."""
        from agents.trading_intelligence import data_access, market_data

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=10.0)
        snapshot = market_data.get_snapshot("NIFTY")
        market_structure = data_access.latest_market_structure("NIFTY")

        profile = rp.classify("NIFTY", snapshot=snapshot, market_structure=market_structure)
        assert profile.trend_regime == "RANGING"


class TestReplay:
    """Milestone 11 Plan's own success criterion for this module: 'computed
    for every symbol every cycle with zero exceptions across a 20+ cycle
    replay test' -- the same shape M10's own test_validation.py::TestReplay
    already established."""

    def test_25_cycle_replay_never_raises_and_stays_internally_consistent(self, ti_db):
        underlying = 24500.0
        for i in range(25):
            underlying += (4 if i % 2 == 0 else -3)
            insert_realistic_chain(
                ti_db, symbol="NIFTY", underlying_ltp=underlying, atm=round(underlying / 50) * 50,
                ts=f"2026-08-06T09:{15 + i}:00",
            )
            insert_market_structure(
                ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15 + i}:00",
                adx=10.0 + (i % 20), atr_14=8.0 + (i % 7),
            )
            profile = rp.classify("NIFTY")
            assert profile.trend_regime in ("TRENDING", "RANGING", "TRANSITIONING", "UNKNOWN")
            assert profile.volatility_regime in ("HIGH", "NORMAL", "LOW", "UNKNOWN")
            if profile.volatility_regime == "UNKNOWN":
                assert profile.volatility_percentile is None
            else:
                assert 0.0 <= profile.volatility_percentile <= 100.0
            assert profile.ce_persistence_cycles >= 0
            assert profile.pe_persistence_cycles >= 0


def _fake_regime(*, trend_regime="TRENDING", adx=30.0):
    return rp.RegimeProfile(
        symbol="TEST", trend_regime=trend_regime, adx=adx, volatility_regime="NORMAL",
        volatility_percentile=50.0, atm_strike=100, ce_buildup_persistent=False,
        pe_buildup_persistent=False, ce_persistence_cycles=0, pe_persistence_cycles=0,
    )


def _rows(strike=100):
    return [StrikeRow(strike=strike, ce_signal="Fresh Call Writing", pe_signal="Neutral")]


class TestPriceStructureFor:
    """Regression coverage for a real production bug (2026-08-24): every
    other call site in this codebase treats data_access.load_candles()'s
    return value as the pandas DataFrame it actually always is (load_candles
    -> backtest.load_intraday_candles(), which returns a DataFrame or an
    empty one, never a plain list). _price_structure_for() alone used
    list-shaped code (`if not candles:`, `for c in candles`) against that
    same DataFrame, which crashed with "The truth value of a DataFrame is
    ambiguous" the first time a real (non-mocked) call reached it -- caught
    silently by failure_gate's shadow-wiring try/except in production
    (SILVERM, TI_ENABLE_FAILURE_GATE_SHADOW). Every test in
    TestClassifyMarketRegimeNSE below monkeypatches _price_structure_for
    itself, so none of them exercised this path -- these tests call the
    real function against a real DataFrame instead."""

    def test_empty_dataframe_is_insufficient_data(self, monkeypatch):
        monkeypatch.setattr(rp.data_access, "load_candles", lambda symbol: pd.DataFrame(columns=["close"]))
        assert rp._price_structure_for("NIFTY") == "INSUFFICIENT_DATA"

    def test_real_dataframe_does_not_crash_and_classifies_correctly(self, monkeypatch):
        candles = pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0]})
        monkeypatch.setattr(rp.data_access, "load_candles", lambda symbol: candles)
        assert rp._price_structure_for("NIFTY") == "HIGHER_HIGH_HIGHER_LOW"

    def test_too_few_candles_is_insufficient_data(self, monkeypatch):
        candles = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
        monkeypatch.setattr(rp.data_access, "load_candles", lambda symbol: candles)
        # below classify_price_structure()'s own 4-reading floor
        assert rp._price_structure_for("NIFTY") == "INSUFFICIENT_DATA"


class TestClassifyMarketRegimeNSE:
    """Post-launch upgrade: Market-Regime/Chop Detection layer. Every test
    here monkeypatches the three building blocks (classify(),
    _price_structure_for(), _breakout_confirmation()) to control the
    exact scenario -- classify_market_regime() itself never reads the DB
    directly, only through those three seams."""

    def test_1_nse_expiry_day_range_chop_detection(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=14.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Premium is not genuinely rising"]))
        today = dt.datetime.now().date()

        result = rp.classify_market_regime(
            "NIFTY", direction="PE", confidence=55, rows=_rows(), atm=100, underlying=99.5,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=today, is_mcx=False,
        )

        assert result.regime == "EXPIRY_CHOP"
        assert result.tradeability == rp.TRADEABILITY_NO_TRADE
        assert result.breakout_override is False
        assert result.is_expiry_day is True
        assert "NO TRADE" in result.reason and "EXPIRY_CHOP" in result.reason

    def test_2_nse_confirmed_bullish_breakout_overrides_chop(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=15.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        today = dt.datetime.now().date()

        result = rp.classify_market_regime(
            "NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=104.9,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=today, is_mcx=False,
        )

        assert result.regime == "BREAKOUT_PENDING"
        assert result.tradeability == rp.TRADEABILITY_CE_CANDIDATE
        assert result.breakout_override is True
        assert "BREAKOUT CONFIRMED" in result.reason

    def test_3_nse_confirmed_bearish_breakdown_overrides_chop(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=15.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        today = dt.datetime.now().date()

        result = rp.classify_market_regime(
            "NIFTY", direction="PE", confidence=70, rows=_rows(), atm=100, underlying=95.1,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=today, is_mcx=False,
        )

        assert result.regime == "BREAKDOWN_PENDING"
        assert result.tradeability == rp.TRADEABILITY_PE_CANDIDATE
        assert result.breakout_override is True
        assert "BREAKDOWN CONFIRMED" in result.reason

    def test_4_mcx_trending_market(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="TRENDING", adx=32.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "HIGHER_HIGH_HIGHER_LOW")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "CRUDEOIL", direction="CE", confidence=80, rows=_rows(), atm=100, underlying=101.0,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=True,
        )

        assert result.regime == "MCX_TRENDING_BULLISH"
        assert result.tradeability == rp.TRADEABILITY_CE_CANDIDATE
        assert result.is_expiry_day is False
        assert result.market == "MCX"

    def test_5_mcx_range_bound_market(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=12.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "CRUDEOIL", direction="PE", confidence=40, rows=_rows(), atm=100, underlying=100.0,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=True,
        )

        assert result.regime == "MCX_RANGE_BOUND"
        assert result.tradeability == rp.TRADEABILITY_NO_TRADE
        assert "EXPIRY_CHOP" not in result.regime  # NSE expiry semantics must never apply to MCX

    def test_6_mcx_breakout(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=15.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))

        result = rp.classify_market_regime(
            "CRUDEOIL", direction="CE", confidence=65, rows=_rows(), atm=100, underlying=104.9,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=True,
        )

        assert result.regime == "MCX_BREAKOUT_PENDING"
        assert result.tradeability == rp.TRADEABILITY_CE_CANDIDATE
        assert result.breakout_override is True
        assert "EXPIRY_CHOP" not in result.reason

    def test_7_mcx_breakdown(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="RANGING", adx=15.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))

        result = rp.classify_market_regime(
            "CRUDEOIL", direction="PE", confidence=65, rows=_rows(), atm=100, underlying=95.1,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=True,
        )

        assert result.regime == "MCX_BREAKDOWN_PENDING"
        assert result.tradeability == rp.TRADEABILITY_PE_CANDIDATE
        assert result.breakout_override is True

    def test_8_weak_momentum_low_adx_nse(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="UNKNOWN", adx=None))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "INSUFFICIENT_DATA")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "NIFTY", direction="CE", confidence=30, rows=_rows(), atm=100, underlying=100.0,
            support=[], resistance=[], market_structure={"atr_14": None}, expiry_date=None, is_mcx=False,
        )

        assert result.regime == "LOW_MOMENTUM"
        assert result.tradeability == rp.TRADEABILITY_NO_TRADE

    def test_8b_weak_momentum_low_adx_mcx(self, monkeypatch):
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="UNKNOWN", adx=None))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "INSUFFICIENT_DATA")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "CRUDEOIL", direction="CE", confidence=30, rows=_rows(), atm=100, underlying=100.0,
            support=[], resistance=[], market_structure={"atr_14": None}, expiry_date=None, is_mcx=True,
        )

        assert result.regime == "MCX_LOW_MOMENTUM"
        assert result.tradeability == rp.TRADEABILITY_NO_TRADE

    def test_9_no_false_blocking_of_a_strong_confirmed_trend_even_on_expiry_day(self, monkeypatch):
        """A genuinely TRENDING market with matching price structure must
        remain tradeable even when today is an NSE expiry day -- the
        filter is a risk gate against CHOP, never a blanket expiry-day
        block (Requirement 3: breakout override / no permanent block)."""
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="TRENDING", adx=35.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "LOWER_HIGH_LOWER_LOW")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))
        today = dt.datetime.now().date()

        result = rp.classify_market_regime(
            "NIFTY", direction="PE", confidence=78, rows=_rows(), atm=100, underlying=94.0,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)], market_structure={"atr_14": 2.0}, expiry_date=today, is_mcx=False,
        )

        assert result.regime == "TRENDING_BEARISH"
        assert result.tradeability == rp.TRADEABILITY_PE_CANDIDATE
        assert result.regime != "EXPIRY_CHOP"

    def test_10_trending_market_with_inconclusive_price_structure_is_not_blocked(self, monkeypatch):
        """Rework (post-replay): classify_price_structure()'s 2-half split
        is coarse and often reads MIXED/INSUFFICIENT_DATA even inside a
        genuinely ADX-confirmed trend (a single pullback candle is enough
        to break a clean HH-HL/LH-LL read over a short window). A strong
        ADX trend must not be discarded into LOW_MOMENTUM just because
        this secondary, noisier check didn't also independently confirm
        the exact same pattern -- only an ACTIVELY CONTRADICTING price
        structure should override a confirmed trend."""
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="TRENDING", adx=30.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "MIXED")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=101.0,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)],
            market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=False,
        )

        assert result.regime == "TRENDING_BULLISH"
        assert result.tradeability == rp.TRADEABILITY_CE_CANDIDATE

    def test_11_trending_market_with_contradicting_price_structure_is_still_blocked(self, monkeypatch):
        """The one case the rework must still catch: ADX says TRENDING, but
        price structure actively shows the OPPOSITE pattern (not just
        inconclusive) -- a genuine disagreement worth staying cautious
        about, not silently overridden."""
        monkeypatch.setattr(rp, "classify", lambda *a, **kw: _fake_regime(trend_regime="TRENDING", adx=30.0))
        monkeypatch.setattr(rp, "_price_structure_for", lambda symbol: "LOWER_HIGH_LOWER_LOW")
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["not evaluated"]))

        result = rp.classify_market_regime(
            "NIFTY", direction="CE", confidence=70, rows=_rows(), atm=100, underlying=101.0,
            support=[StrikeRow(strike=95)], resistance=[StrikeRow(strike=105)],
            market_structure={"atr_14": 2.0}, expiry_date=None, is_mcx=False,
        )

        assert result.regime != "TRENDING_BULLISH"
        assert result.tradeability != rp.TRADEABILITY_CE_CANDIDATE


class TestRegimeFilterNoStrategyOrBrokerImpact:
    """Requirements 11/12 (test categories) -- the regime filter must never
    invent a broker order, and (via ai_trading_engine's shadow wiring)
    must never change an existing Recommendation's real trading fields."""

    def test_11_no_broker_names_anywhere_in_the_new_code(self):
        import ast
        import inspect

        forbidden = {"SmartConnect", "smartApi", "AngelOneFetcher", "_shared_angel_fetcher",
                     "place_order", "placeOrder", "modifyOrder", "cancelOrder"}
        source = inspect.getsource(rp)
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (names & forbidden), f"forbidden broker names found: {names & forbidden}"

    def test_12_shadow_mode_never_changes_the_real_trading_fields(self, monkeypatch, ti_db):
        """Same signal, same everything -- flag OFF vs flag ON must
        produce byte-identical action/direction/entry/target/sl/qty on
        the Recommendation. Only the new regime_* fields may differ."""
        from agents import config as agents_config
        from agents.trading_intelligence import ai_trading_engine, market_data

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_market_structure(ti_db, symbol="NIFTY", adx=30.0, atr_14=10.0)

        monkeypatch.setattr(agents_config, "TI_ENABLE_REGIME_FILTER_SHADOW", False)
        snapshot = market_data.get_snapshot("NIFTY")
        rec_off = ai_trading_engine.evaluate("NIFTY", snapshot=snapshot, capital=500000, risk_pct=1.0)

        monkeypatch.setattr(agents_config, "TI_ENABLE_REGIME_FILTER_SHADOW", True)
        rec_on = ai_trading_engine.evaluate("NIFTY", snapshot=snapshot, capital=500000, risk_pct=1.0)

        assert rec_off.action == rec_on.action
        assert rec_off.direction == rec_on.direction
        assert rec_off.entry_price == rec_on.entry_price
        assert rec_off.target_price == rec_on.target_price
        assert rec_off.sl_price == rec_on.sl_price
        assert rec_off.qty == rec_on.qty
        assert rec_off.market_regime is None  # flag off -- shadow never computed
        # flag on: either populated (an actionable signal fired) or still
        # None (this cycle happened to be HOLD/NO_TRADE) -- both honest.

    def test_flag_off_never_populates_regime_fields(self, monkeypatch, ti_db):
        from agents import config as agents_config
        from agents.trading_intelligence import ai_trading_engine, market_data

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        monkeypatch.setattr(agents_config, "TI_ENABLE_REGIME_FILTER_SHADOW", False)

        rec = ai_trading_engine.evaluate("NIFTY", capital=500000, risk_pct=1.0)

        assert rec.market_regime is None
        assert rec.regime_tradeability is None
        assert rec.regime_reason is None
        assert rec.regime_breakout_override is None

    def test_a_bug_in_regime_shadow_wiring_never_breaks_the_real_recommendation(self, monkeypatch, ti_db):
        from agents import config as agents_config
        from agents.trading_intelligence import ai_trading_engine

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        monkeypatch.setattr(agents_config, "TI_ENABLE_REGIME_FILTER_SHADOW", True)
        monkeypatch.setattr(
            rp, "classify_market_regime",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        rec = ai_trading_engine.evaluate("NIFTY", capital=500000, risk_pct=1.0)

        assert rec is not None
        assert rec.market_regime is None
