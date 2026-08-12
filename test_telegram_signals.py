"""
test_telegram_signals.py -- Milestone 18: regression tests for the
"IDaddy Scalping Signals" Telegram channel feature -- get_sr_trade_trigger()'s
new target2/target3/three_targets_achievable fields, and the three pure
message formatters (format_signal_open_message/format_signal_progress_message/
format_signal_close_message). All of these are pure functions (no DB, no
network) -- send_telegram_channel() itself is exercised only for its
no-op-when-unconfigured contract, never a real HTTP call.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import app


class TestGetSrTradeTrigger:
    def _make_active_level(self, **overrides):
        st = {
            "state": "ACTIVE", "triggered": True, "trade_opened": False, "risk_reward_ok": True,
            "entry_strike": 24500, "direction_opt": "CE", "entry_price": 100.0,
            "target1": 115.0, "target2": 130.0, "sl": 95.0, "risk_reward": 3.0,
            "institutional_score": 80, "institutional_tier": "HIGH",
        }
        st.update(overrides)
        return st

    def test_surfaces_target2_and_extrapolated_target3_when_achievable(self, monkeypatch):
        monkeypatch.setitem(app.state["sr_state_by_symbol"], "NIFTY", {"resistance_1": self._make_active_level()})
        monkeypatch.setattr(app, "get_sr_live_params", lambda symbol: {"min_risk_reward": 1.5})

        trigger = app.get_sr_trade_trigger("NIFTY")

        assert trigger["target1"] == 115.0
        assert trigger["target2"] == 130.0
        # target3 = target2 + (target2 - target1) = 130 + 15 = 145
        assert trigger["target3"] == 145.0
        # RR to target3 = (145-100)/(100-95) = 9.0 -- comfortably clears min_rr=1.5
        assert trigger["three_targets_achievable"] is True
        # target_price (the field the real exit logic reads) is still target1, unchanged.
        assert trigger["target_price"] == 115.0

    def test_collapses_to_target1_only_when_target3_fails_rr_bar(self, monkeypatch):
        # A much tighter target1/target2 spacing relative to risk -- target3's
        # reward/risk no longer clears a strict min_rr bar.
        st = self._make_active_level(target1=101.0, target2=102.0, sl=95.0)
        monkeypatch.setitem(app.state["sr_state_by_symbol"], "NIFTY", {"resistance_1": st})
        monkeypatch.setattr(app, "get_sr_live_params", lambda symbol: {"min_risk_reward": 5.0})

        trigger = app.get_sr_trade_trigger("NIFTY")

        assert trigger["target3"] == 103.0
        assert trigger["three_targets_achievable"] is False

    def test_marks_level_as_trade_opened_so_it_never_reopens(self, monkeypatch):
        st = self._make_active_level()
        monkeypatch.setitem(app.state["sr_state_by_symbol"], "NIFTY", {"resistance_1": st})
        monkeypatch.setattr(app, "get_sr_live_params", lambda symbol: {"min_risk_reward": 1.5})

        first = app.get_sr_trade_trigger("NIFTY")
        second = app.get_sr_trade_trigger("NIFTY")

        assert first is not None
        assert second is None
        assert st["trade_opened"] is True


class TestFormatSignalOpenMessage:
    def _trigger(self, **overrides):
        base = {
            "strike": 24500, "direction": "CE", "entry_price": 100.0,
            "target1": 115.0, "target2": 130.0, "target3": 145.0,
            "three_targets_achievable": True, "sl_price": 95.0,
        }
        base.update(overrides)
        return base

    def test_ce_is_labeled_buy_and_shows_all_three_targets_when_achievable(self):
        msg = app.format_signal_open_message("NIFTY", self._trigger())
        assert "BUY SIGNAL" in msg
        assert "NIFTY 24500 CE" in msg
        assert "Buy @ 100.0" in msg
        assert "Target 1 @ 115.0" in msg
        assert "Target 2 @ 130.0" in msg
        assert "Target 3 @ 145.0" in msg
        assert "SL @ 95.0" in msg

    def test_pe_is_labeled_sell(self):
        msg = app.format_signal_open_message("NIFTY", self._trigger(direction="PE"))
        assert "SELL SIGNAL" in msg
        assert "Buy @ 100.0" in msg  # still literally buying the put premium

    def test_shows_only_target_1_when_not_achievable(self):
        msg = app.format_signal_open_message("NIFTY", self._trigger(three_targets_achievable=False))
        assert "Target 1 @ 115.0" in msg
        assert "Target 2" not in msg
        assert "Target 3" not in msg
        assert "SL @ 95.0" in msg


class TestFormatSignalProgressMessage:
    def test_reports_percent_of_the_way_to_target(self):
        open_trade = {"strike": 24500, "direction": "CE", "entry_price": 100.0,
                       "target_price": 120.0, "current_price": 110.0}
        msg = app.format_signal_progress_message("NIFTY", open_trade)
        assert "LTP 110.0" in msg
        assert "50% to Target 1 @ 120.0" in msg

    def test_clamps_to_100_percent_if_ltp_already_past_target(self):
        open_trade = {"strike": 24500, "direction": "CE", "entry_price": 100.0,
                       "target_price": 120.0, "current_price": 150.0}
        msg = app.format_signal_progress_message("NIFTY", open_trade)
        assert "100% to Target 1" in msg


class TestFormatSignalCloseMessage:
    def test_reports_points_and_lot_size_pnl(self, monkeypatch):
        monkeypatch.setattr(app, "PAPER_TRADE_LOT_QTY", 50)
        open_trade = {"strike": 24500, "direction": "CE", "exit_price": 120.0}
        msg = app.format_signal_close_message("NIFTY", open_trade, "TARGET HIT", 20.0)
        assert "EXIT — NIFTY 24500CE" in msg
        assert "Exit @ 120.0 (TARGET HIT)" in msg
        assert "Points: +20.00" in msg
        assert "Lot Qty: 50" in msg
        assert "P&L: ₹+1000.0" in msg


class TestSendTelegramChannelNoOpsWhenUnconfigured:
    def test_returns_false_without_channel_id(self, monkeypatch):
        monkeypatch.setattr(app, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(app, "TELEGRAM_SIGNALS_CHANNEL_ID", "")
        assert app.send_telegram_channel("test") is False

    def test_returns_false_without_bot_token(self, monkeypatch):
        monkeypatch.setattr(app, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(app, "TELEGRAM_SIGNALS_CHANNEL_ID", "@somechannel")
        assert app.send_telegram_channel("test") is False
