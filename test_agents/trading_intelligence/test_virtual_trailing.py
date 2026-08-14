"""
test_agents/trading_intelligence/test_virtual_trailing.py -- Milestone
21, Phase 1: regression tests for virtual_trailing.py, the paper-trade /
advisory-only shadow trailing engine. Covers the pure evaluate_trade()
state machine (breakeven trigger, trail tiers, ATR floor, runner mode,
capital protection, exit condition), persistence round-trips, and the
per-cycle orchestration against a real (test-shaped) DB via the shared
ti_db fixture.
"""
import pytest

from agents.trading_intelligence import ti_store, virtual_trailing as vt
from test_agents.trading_intelligence.conftest import insert_cycle, insert_market_structure, insert_strike


def _fresh_state(*, entry_price=100.0, sl_price=90.0, target_price=120.0, direction="CE"):
    trade = {
        "id": 1, "symbol": "NIFTY", "direction": direction, "entry_price": entry_price,
        "sl_price": sl_price, "target_price": target_price,
    }
    return vt._init_state(trade, now="2026-08-14T09:15:00")


class TestEvaluateTradeBelowBreakeven:
    def test_gain_below_8_leaves_virtual_sl_at_original(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 105.0)   # gain = 5
        assert new["virtual_sl"] == 90.0
        assert new["locked_profit"] == 0.0
        assert new["state"] == "ACTIVE"

    def test_exit_below_breakeven_uses_original_stop_loss_reason(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 90.0)   # falls straight to the original SL, gain never reached 8
        assert new["state"] == "EXITED"
        assert new["exit_reason"] == "VIRTUAL STOP LOSS"
        assert new["exit_price"] == 90.0


class TestEvaluateTradeTrailingTiers:
    def test_plus_8_moves_sl_to_cost(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 109.0)   # gain = 9
        assert new["virtual_sl"] == 100.0
        assert new["locked_profit"] == 0.0

    def test_plus_12_trails_by_4(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 113.0)   # gain = 13
        assert new["virtual_sl"] == 109.0   # 113 - 4
        assert new["locked_profit"] == 9.0

    def test_plus_20_trails_by_6(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 121.0)   # gain = 21
        assert new["virtual_sl"] == 115.0   # 121 - 6
        assert new["locked_profit"] == 15.0

    def test_plus_30_trails_by_10_and_enters_runner_mode(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 131.0)   # gain = 31
        assert new["virtual_sl"] == 121.0   # 131 - 10
        assert new["runner_mode"] is True
        assert new["virtual_target"] is None   # uncapped once in runner mode

    def test_exit_fires_when_premium_falls_back_by_the_trail_amount(self):
        state = _fresh_state()
        state = vt.evaluate_trade(state, 113.0)   # engages the +12 tier, virtual_sl=109
        assert state["state"] == "ACTIVE"
        state = vt.evaluate_trade(state, 109.0)   # falls back to the trailing stop exactly
        assert state["state"] == "EXITED"
        assert state["exit_reason"] == "VIRTUAL TRAILING STOP"
        assert state["exit_price"] == 109.0


class TestATRBasedTrailing:
    def test_a_wide_atr_widens_the_trail_amount_beyond_the_fixed_rule(self):
        state = _fresh_state()
        tight = vt.evaluate_trade(state, 113.0, atr_14=None)          # fixed rule: trail by 4 -> sl=109
        wide = vt.evaluate_trade(state, 113.0, atr_14=20.0)           # 0.5 * 20 = 10 > 4 -> sl=103
        assert tight["virtual_sl"] == 109.0
        assert wide["virtual_sl"] == 103.0

    def test_a_narrow_atr_never_tightens_below_the_fixed_rule(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 113.0, atr_14=2.0)   # 0.5 * 2 = 1 < fixed rule amount 4
        assert new["virtual_sl"] == 109.0   # fixed 4-point rule still applies, never tighter than the rule

    def test_breakeven_tier_is_never_atr_scaled(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 109.0, atr_14=50.0)   # gain=9, breakeven tier only
        assert new["virtual_sl"] == 100.0   # exactly cost, unaffected by a large ATR


class TestCapitalProtectionInvariant:
    def test_virtual_sl_never_moves_backward_on_a_pullback_that_does_not_breach_it(self):
        state = _fresh_state()
        state = vt.evaluate_trade(state, 121.0)   # gain=21 -> sl=115
        assert state["virtual_sl"] == 115.0
        state = vt.evaluate_trade(state, 117.0)   # pulls back but doesn't breach 115 -- highest_premium unchanged
        assert state["virtual_sl"] == 115.0   # still 115, never regresses
        assert state["state"] == "ACTIVE"

    def test_locked_profit_never_reported_negative_pre_breakeven(self):
        state = _fresh_state()
        new = vt.evaluate_trade(state, 95.0)   # below entry, gain never reached 8
        assert new["locked_profit"] == 0.0


class TestFrozenAfterExit:
    def test_an_exited_state_is_never_re_evaluated(self):
        state = _fresh_state()
        state = vt.evaluate_trade(state, 90.0)   # exits immediately (below original SL)
        assert state["state"] == "EXITED"
        frozen = vt.evaluate_trade(state, 999.0)   # a huge favorable move afterward changes nothing
        assert frozen == state


class TestPersistence:
    def test_get_state_returns_none_before_any_write(self, ti_db):
        vt.init_db()
        assert vt.get_state(999) is None

    def test_upsert_then_get_round_trips(self, ti_db):
        vt.init_db()
        state = _fresh_state()
        vt.upsert_state(state)
        loaded = vt.get_state(state["trade_id"])
        assert loaded["symbol"] == "NIFTY"
        assert loaded["entry_price"] == 100.0
        assert loaded["runner_mode"] is False

    def test_upsert_is_idempotent_via_trade_id(self, ti_db):
        vt.init_db()
        state = _fresh_state()
        vt.upsert_state(state)
        updated = vt.evaluate_trade(state, 113.0)
        vt.upsert_state(updated)
        rows = vt.list_states()
        assert len(rows) == 1
        assert rows[0]["virtual_sl"] == 109.0

    def test_list_states_filters_by_symbol_and_active_only(self, ti_db):
        vt.init_db()
        active = _fresh_state()
        vt.upsert_state(active)
        exited = dict(active, trade_id=2, symbol="BANKNIFTY")
        exited = vt.evaluate_trade(exited, 90.0)
        vt.upsert_state(exited)

        assert len(vt.list_states()) == 2
        assert len(vt.list_states(symbol="NIFTY")) == 1
        assert len(vt.list_states(active_only=True)) == 1
        assert vt.list_states(active_only=True)[0]["symbol"] == "NIFTY"


class TestRunVirtualTrailingCycle:
    def test_initializes_state_for_a_real_open_trade_and_tracks_it(self, ti_db):
        vt.init_db()
        cid = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-14T09:16:00", underlying_ltp=24510.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500, ce_ltp=113.0)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-14T09:16:00", atr_14=10.0)

        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )

        findings = vt.run_virtual_trailing_cycle(["NIFTY"])

        state = vt.get_state(trade_id)
        assert state is not None
        assert state["virtual_sl"] == 108.0   # gain=13 -> +12 tier: fixed rule trails by 4, but atr_14=10.0's
        # 0.5x floor (5) is wider, so the ATR floor wins: 113 - 5 = 108
        assert findings == []   # no exit yet

    def test_a_virtual_exit_this_cycle_is_reported_as_a_finding(self, ti_db):
        vt.init_db()
        cid1 = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-14T09:15:00", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid1, 24500, ce_ltp=113.0)

        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )
        vt.run_virtual_trailing_cycle(["NIFTY"])   # engages the +12 tier, virtual_sl=109

        cid2 = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-14T09:17:00", underlying_ltp=24500.0, atm=24500.0)
        insert_strike(ti_db, cid2, 24500, ce_ltp=109.0)   # falls back onto the trailing stop
        findings = vt.run_virtual_trailing_cycle(["NIFTY"])

        assert len(findings) == 1
        assert f"trade #{trade_id}" in findings[0]["summary"]
        state = vt.get_state(trade_id)
        assert state["state"] == "EXITED"

    def test_a_trade_with_no_recorded_premium_is_skipped_not_errored(self, ti_db):
        vt.init_db()
        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=99999, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )
        findings = vt.run_virtual_trailing_cycle(["NIFTY"])
        assert findings == []
        assert vt.get_state(trade_id) is None

    def test_state_is_frozen_once_the_real_trade_closes(self, ti_db):
        vt.init_db()
        cid1 = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-14T09:15:00", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid1, 24500, ce_ltp=113.0)

        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )
        vt.run_virtual_trailing_cycle(["NIFTY"])
        before = vt.get_state(trade_id)

        ti_store.close_trade(trade_id, exit_price=120.0, exit_reason="TARGET HIT")   # the real trade closes
        cid2 = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-14T09:20:00", underlying_ltp=24600.0, atm=24500.0)
        insert_strike(ti_db, cid2, 24500, ce_ltp=140.0)   # a huge further move -- should never be seen now
        vt.run_virtual_trailing_cycle(["NIFTY"])   # trade no longer OPEN -- not in ti_store.list_open_trades()

        after = vt.get_state(trade_id)
        assert after == before


class TestCurrentPremiumScope:
    """Milestone 21 Phase 2 data-integrity audit finding (documents
    intended behavior, not a bug): current_premium() reads
    data_access.recent_strike_history(symbol, strike, limit=1) -- keyed by
    (symbol, strike) only, direction picked from that same row afterward.
    This is the correct granularity for an options premium (per-contract,
    always freshly read, never cached on the row) -- but it means it is
    NOT scoped by trade_id: two different virtual_trailing_state rows
    (different trade_ids, different entry_price/highest_premium history)
    that happen to share the same (symbol, strike, direction) -- common,
    since the AI Trading Engine often re-enters the same near-ATM strike
    across multiple trades over a session -- report the IDENTICAL
    current_premium. Production evidence: SILVER trade_ids 33/59/62 all
    share strike 237000/CE with wildly different entry_price
    (5840/3947/5688.5); the "current LTP" shown for the trade_id=59 row
    (entry 3947) is the live strike-237000 premium, not anything computed
    from that specific trade's own history -- which is why it can equal
    another row's entry_price by coincidence, not corruption."""

    def test_two_trades_on_the_same_strike_share_the_same_current_premium(self, ti_db):
        vt.init_db()
        cid = insert_cycle(ti_db, symbol="SILVER", ts="2026-08-14T20:40:00", underlying_ltp=118000.0, atm=237000.0)
        insert_strike(ti_db, cid, 237000, ce_ltp=5688.5)

        old_trade = {"symbol": "SILVER", "direction": "CE", "strike": 237000, "entry_price": 3947.0}
        new_trade = {"symbol": "SILVER", "direction": "CE", "strike": 237000, "entry_price": 5688.5}

        assert vt.current_premium(old_trade) == vt.current_premium(new_trade) == 5688.5
