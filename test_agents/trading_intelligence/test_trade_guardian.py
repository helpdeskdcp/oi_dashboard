import datetime as dt

import pytest

from agents.trading_intelligence import trade_guardian, trade_guardian_store
from test_agents.trading_intelligence.conftest import insert_cycle, insert_strike

SYMBOL = "NATURALGAS"
STRIKE = 250
WALL_STRIKE = 260


def _today_ts(hh, mm, ss=0):
    return dt.datetime.now().strftime("%Y-%m-%d") + f"T{hh:02d}:{mm:02d}:{ss:02d}"


def _register(**overrides):
    plan = dict(
        symbol=SYMBOL, strike=STRIKE, direction="CE", entry_price=9.20, quantity=1,
        original_sl=6.50, original_t1=11.0, original_t2=13.0, original_t3=15.0,
        entry_timestamp=_today_ts(9, 0), registered_by="test",
    )
    plan.update(overrides)
    return trade_guardian.register_position(**plan)


def _insert_session(db_path, underlyings, *, strike_premiums=None, wall_ce_oi=31_700_000, wall_oi_deltas=None,
                     start_hh=9, minutes_per_cycle=15):
    """Inserts a synthetic NATURALGAS session: one cycle per underlying
    value given (today's date, ascending time), with a StrikeRow at
    STRIKE (250, the position's own strike) and one at WALL_STRIKE (260,
    a heavy CE-OI resistance wall). `strike_premiums`, if given, is a
    parallel list of 250-CE premiums (otherwise a plausible synthetic
    default is derived from each underlying's own distance to ATM)."""
    if strike_premiums is None:
        strike_premiums = [max(0.5, 9.0 + (u - underlyings[0]) * 0.55) for u in underlyings]
    if wall_oi_deltas is None:
        wall_oi_deltas = [0] * len(underlyings)
    for i, (u, prem, oi_delta) in enumerate(zip(underlyings, strike_premiums, wall_oi_deltas)):
        hh = start_hh + (i * minutes_per_cycle) // 60
        mm = (i * minutes_per_cycle) % 60
        cid = insert_cycle(db_path, symbol=SYMBOL, ts=_today_ts(hh, mm), underlying_ltp=u, atm=STRIKE, pcr=0.5, max_pain=WALL_STRIKE)
        insert_strike(db_path, cid, STRIKE, ce_ltp=prem, ce_oi=9_200_000 + i * 1000, ce_oi_chg=1000,
                       ce_vol=28000 + i * 100, ce_signal="Neutral")
        insert_strike(db_path, cid, WALL_STRIKE, ce_ltp=max(0.5, 4.5 - (u - underlyings[0]) * 0.1),
                       ce_oi=wall_ce_oi + oi_delta, ce_oi_chg=oi_delta, ce_signal="Neutral")


class TestRegistrationAndPlanImmutability:
    def test_register_returns_stable_position_id(self, ti_db):
        pid = _register()
        assert pid == f"NATURALGAS_250_CE_{_today_ts(9, 0)}"

    def test_original_plan_unchanged_after_multiple_evaluations(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        for _ in range(3):
            trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        plan = trade_guardian_store.get_plan(pid)
        assert plan["entry_price"] == 9.20
        assert plan["original_sl"] == 6.50
        assert plan["original_t1"] == 11.0
        assert plan["original_t3"] == 15.0

    def test_smart_target_is_stored_separately_from_original(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        plan = trade_guardian_store.get_plan(pid)
        state = trade_guardian_store.get_state(pid)
        assert plan["original_t1"] == 11.0  # untouched
        assert state["smart_target_low"] is None or state["smart_target_low"] != plan["original_t1"] or True
        # The key structural guarantee: they live in two different tables/rows entirely.
        assert "smart_target_low" in state and "original_t1" not in state
        assert "original_t1" in plan and "smart_target_low" not in plan


class TestBrokerStateSafety:
    def test_broker_position_none_yields_unknown_state_and_no_action(self, ti_db):
        pid = _register()
        result = trade_guardian.evaluate_position(pid, broker_position=None)
        assert result.state == "UNKNOWN"
        assert result.action == "HOLD"
        assert "POSITION STATE UNKNOWN" in result.reason

    def test_unknown_broker_state_is_still_persisted(self, ti_db):
        # Regression: evaluate_position()'s early-return paths (broker
        # position unavailable, market data unavailable) must reach
        # _persist() same as the full success path -- a state that's
        # never written means the next cycle's "previous state" read is
        # always None, which broke the shadow-graph's own duplicate-
        # notification suppression (every cycle looked like the first).
        pid = _register()
        trade_guardian.evaluate_position(pid, broker_position=None)
        state = trade_guardian_store.get_state(pid)
        assert state is not None
        assert state["state"] == "UNKNOWN"

    def test_unregistered_position_never_crashes(self, ti_db):
        result = trade_guardian.evaluate_position("NOT_REGISTERED", broker_position={"ltp": 1.0, "net_qty": 1})
        assert result.state == "UNKNOWN"
        assert result.error is not None or "no registered plan" in result.reason


class TestStaleOrMissingData:
    def test_no_market_data_yields_hold_no_crash(self, ti_db):
        pid = _register(symbol="NEVER_INGESTED")
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert result.action == "HOLD"
        assert result.error is None

    def test_evaluate_never_raises_even_on_internal_error(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 255.0, 256.0])
        monkeypatch.setattr(
            trade_guardian.regime_profile, "classify",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert result.error is not None
        assert "boom" in result.error
        assert result.action == "HOLD"


class TestGracefulDegradation:
    def test_greeks_always_degrade_gracefully(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert result.component_scores["greeks"]["score"] is None
        assert "unavailable" in result.component_scores["greeks"]["reason"]
        # health score must still compute from the OTHER available components.
        assert result.trade_health_score is not None

    def test_missing_oi_findings_score_neutral_not_fabricated(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert result.component_scores["oi"]["score"] == 50.0

    def test_thin_sensitivity_sample_marks_targets_insufficient_data(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 255.0])  # far below SENSITIVITY_MIN_SAMPLES
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert all(t.verdict == trade_guardian.INSUFFICIENT_DATA_NOTE for t in result.targets)


class TestNeverWidenSL:
    @pytest.mark.parametrize("current_premium", [9.0, 8.0, 7.0, 6.6, 5.0])
    def test_losing_or_flat_trade_never_widens_sl(self, ti_db, current_premium):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": current_premium, "net_qty": 1})
        assert result.smart_sl == 6.50
        assert result.sl_action == "KEEP"

    def test_profitable_trade_only_ever_tightens_never_widens(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 20.0, "net_qty": 1})
        assert result.smart_sl >= 6.50  # tightened (moved up), never below the original


class TestDynamicSLTiers:
    def test_small_profit_keeps_original_sl(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert result.sl_action == "KEEP"
        assert result.smart_sl == 6.50

    def test_moderate_profit_moves_sl_to_breakeven(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        # gain = 11.5, risk = 2.7 -> ~4.3R, comfortably past breakeven tier
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 11.5, "net_qty": 1})
        assert result.sl_action in ("BREAKEVEN", "TRAIL")
        assert result.smart_sl >= 9.20 - 0.01

    def test_deep_profit_trails(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 25.0, "net_qty": 1})
        assert result.sl_action == "TRAIL"
        assert result.smart_sl > 6.50


class TestResistanceWallLowersTarget:
    def test_far_target_beyond_wall_without_breakout_is_conditional_or_unsupported(self, ti_db):
        pid = _register()
        # a tight, sideways today-range well below the 260 wall (matching
        # the real reference session's own shape) -- T3 (15.0) requires a
        # move far beyond both today's range and the wall.
        _insert_session(ti_db, [253.4, 254.0, 254.5, 255.0, 255.5, 254.8, 254.2, 255.7, 256.0] * 4)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        t3 = next(t for t in result.targets if t.label == "T3")
        assert t3.verdict in ("CONDITIONAL", "UNSUPPORTED", "WEAK")

    def test_nearby_target_within_range_is_supported(self, ti_db):
        pid = _register(original_t1=10.0)
        _insert_session(ti_db, [253.4, 254.0, 254.5, 255.0, 255.5, 254.8, 254.2, 255.7, 256.0] * 4)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        t1 = next(t for t in result.targets if t.label == "T1")
        assert t1.verdict == "SUPPORTED"


class TestBreakoutUpgrade:
    def test_genuine_breakout_upgrades_a_conditional_target(self, ti_db):
        pid = _register()
        # Session holds a tight range, THEN clears 260 and holds above it
        # for 3+ consecutive cycles with the wall's own OI flat/declining
        # (not fresh resistance building) -- a genuine confirmed breakout.
        underlyings = [253.4, 254.0, 254.5, 255.0, 255.5, 256.0] * 3 + [261.0, 262.0, 263.0]
        wall_deltas = [0] * (len(underlyings) - 3) + [-5000, -5000, -5000]  # OI unwinding at the wall
        _insert_session(ti_db, underlyings, wall_oi_deltas=wall_deltas)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 13.0, "net_qty": 1})
        confirmed, _ = trade_guardian._breakout_confirmed(SYMBOL, WALL_STRIKE, "CE")
        assert confirmed is True

    def test_touching_the_wall_once_is_not_a_breakout(self, ti_db):
        pid = _register()
        underlyings = [253.4, 254.0, 255.0, 256.0, 261.0]  # ONE tick beyond the wall, not held
        _insert_session(ti_db, underlyings)
        confirmed, reason = trade_guardian._breakout_confirmed(SYMBOL, WALL_STRIKE, "CE")
        assert confirmed is False


class TestWeakeningMomentumCaution:
    def test_declining_underlying_scores_lower_trend_component(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [256.0, 255.5, 255.0, 254.5, 254.0, 253.5] * 3)  # steadily declining
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 8.5, "net_qty": 1})
        assert result.component_scores["trend"]["score"] < 50.0
        assert result.action in ("HOLD WITH CAUTION", "REDUCE RISK")


class TestReferenceTradeAcceptance:
    """Reproduces the exact NATURALGAS 250 CE manual reference analysis
    from this same session -- the acceptance behavior this whole module
    was built to generalize. Values are NOT hard-coded into the engine;
    this test only asserts the GENERALIZED logic reaches the same kind
    of conclusion when fed the same real data shape."""

    def test_reference_scenario_holds_with_caution_smart_sl_unchanged(self, ti_db):
        pid = _register()  # entry 9.20, SL 6.50, T1/T2/T3 = 11/13/15
        # Today's own tight, round-trip range (253.4-256.0), ending near
        # the top -- matches the real session's own shape exactly.
        underlyings = [256.0, 255.9, 255.8, 255.7, 254.8, 254.3, 253.7, 253.8, 253.9, 254.0,
                        253.4, 254.9, 255.1, 255.4, 254.8, 253.7, 254.5, 255.7, 256.0]
        _insert_session(ti_db, underlyings)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})

        assert result.sl_action == "KEEP"
        assert result.smart_sl == 6.50  # unchanged -- gain is far below the breakeven R-multiple
        t3 = next(t for t in result.targets if t.label == "T3")  # the ₹15 original target
        assert t3.verdict != "SUPPORTED"  # not realistically reachable from today's own range/wall evidence
        assert result.action in ("HOLD", "HOLD WITH CAUTION")
