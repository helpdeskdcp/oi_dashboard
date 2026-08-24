"""
test_agents/trading_intelligence/test_telegram_notifier.py -- Milestone 19:
regression tests for agents/trading_intelligence/telegram_notifier.py, the
Trading-Intelligence-only Telegram signal sender that replaced the
Milestone 18 S/R Engine wiring. Pure unit tests -- no DB, no real HTTP
(requests.post is monkeypatched), matching this package's own convention
of never importing app.py from test_agents/ (see conftest.py's own
docstring).
"""
import datetime as dt

import pytest

from agents.trading_intelligence import telegram_notifier as tn


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    tn._last_sent_by_fingerprint.clear()
    tn._last_structure_update_by_fingerprint.clear()
    tn._last_guardian_update_by_fingerprint.clear()
    yield
    tn._last_sent_by_fingerprint.clear()
    tn._last_structure_update_by_fingerprint.clear()
    tn._last_guardian_update_by_fingerprint.clear()


class _FakeResponse:
    def raise_for_status(self):
        pass


EXAMPLE_PAYLOAD = {
    "symbol": "NIFTY", "signal_type": "BUY_CE", "overall_bias": "BULLISH", "confidence": 82,
    "entry_zone": {"strike": 24900, "price": 118}, "targets": [132, 148, 166], "stop_loss": 106,
    "institutional_score": 78, "premium_momentum": "STRONG", "oi_structure": "BULLISH",
    "vwap_structure": "ABOVE", "repeated_rejection": True,
    "expiry_date": "2026-08-06", "trading_symbol": "NIFTY06AUG2624900CE",
}


class TestSendTradingIntelligenceSignal:
    def test_returns_false_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "")
        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is False

    def test_sends_exactly_once_for_the_example_payload(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []

        def fake_post(url, json, timeout):
            calls.append((url, json, timeout))
            return _FakeResponse()

        monkeypatch.setattr(tn.requests, "post", fake_post)

        result = tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD)

        assert result is True
        assert len(calls) == 1
        url, body, _ = calls[0]
        assert "dummy-token" in url
        assert body["chat_id"] == "-1003927831776"
        assert body["parse_mode"] == "HTML"
        assert "NIFTY" in body["text"]

    def test_returns_false_and_does_not_send_when_post_raises(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")

        def raising_post(*a, **kw):
            raise ConnectionError("boom")

        monkeypatch.setattr(tn.requests, "post", raising_post)

        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is False


class TestDuplicateSuppression:
    def test_identical_signal_within_5_minutes_is_suppressed(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: calls.append(1) or _FakeResponse())

        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is True
        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is False
        assert len(calls) == 1

    def test_a_different_strike_is_not_suppressed(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: calls.append(1) or _FakeResponse())

        other = dict(EXAMPLE_PAYLOAD, entry_zone={"strike": 25000, "price": 118})
        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is True
        assert tn.send_trading_intelligence_signal(other) is True
        assert len(calls) == 2

    def test_suppression_expires_after_the_dedup_window(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        fingerprint = tn._signal_fingerprint(EXAMPLE_PAYLOAD)
        tn._last_sent_by_fingerprint[fingerprint] = dt.datetime.now() - dt.timedelta(seconds=tn.DEDUP_WINDOW_SECONDS + 1)
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: calls.append(1) or _FakeResponse())

        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is True
        assert len(calls) == 1


class TestFingerprint:
    def test_reads_strike_and_price_from_entry_zone_dict(self):
        fp = tn._signal_fingerprint(EXAMPLE_PAYLOAD)
        assert fp == ("NIFTY", "BUY_CE", 24900, 118.0)

    def test_reads_strike_and_price_from_flat_keys(self):
        payload = {"symbol": "NIFTY", "signal_type": "BUY_PE", "strike": 24500, "entry_price": 12.345}
        fp = tn._signal_fingerprint(payload)
        assert fp == ("NIFTY", "BUY_PE", 24500, 12.3)


class TestFormatHtml:
    def test_includes_all_supplied_ai_factor_fields(self):
        msg = tn._format_html(EXAMPLE_PAYLOAD)
        assert "<b>IDaddy AI Trading Intelligence</b>" in msg
        assert "NIFTY" in msg
        assert "BULLISH" in msg
        assert "82%" in msg
        assert "BUY <b>NIFTY 24900 CE</b>" in msg
        assert "Expiry: <b>06-Aug-2026</b>" in msg
        assert "Contract: <b>NIFTY06AUG2624900CE</b>" in msg
        assert "Above: <b>118</b>" in msg
        assert "T1: 132" in msg
        assert "T2: 148" in msg
        assert "T3: 166" in msg
        assert "106" in msg  # stop loss
        assert "Institutional Score: 78" in msg
        assert "Premium Momentum: STRONG" in msg
        assert "OI Structure: BULLISH" in msg
        assert "VWAP: ABOVE" in msg
        assert "Repeated Rejection: YES" in msg

    def test_omits_ai_factor_lines_that_were_not_supplied(self):
        minimal = {
            "symbol": "BANKNIFTY", "signal_type": "BUY_PE", "overall_bias": "BEARISH", "confidence": 80,
            "entry_zone": {"strike": 51000, "price": 90}, "targets": [100], "stop_loss": 80,
        }
        msg = tn._format_html(minimal)
        assert "Institutional Score" not in msg
        assert "Premium Momentum" not in msg
        assert "OI Structure" not in msg
        assert "VWAP" not in msg
        assert "Repeated Rejection" not in msg

    def test_renders_reasoning_details_as_bullets(self):
        payload = dict(EXAMPLE_PAYLOAD, reasoning_details=["OI supports bulls", "Greeks aligned"])
        del payload["institutional_score"], payload["premium_momentum"]
        del payload["oi_structure"], payload["vwap_structure"], payload["repeated_rejection"]
        msg = tn._format_html(payload)
        assert "• OI supports bulls" in msg
        assert "• Greeks aligned" in msg

    def test_falls_back_to_top_level_reasoning_keys_when_reasoning_details_absent(self):
        payload = {
            "symbol": "NIFTY", "signal_type": "BUY CE", "overall_bias": "BULLISH", "confidence": 85,
            "entry_zone": {"strike": 24500, "price": 120.5}, "targets": [145, 165, 188], "stop_loss": 95,
            "institutional_reasoning": "Institutional bias supports the bullish continuation setup.",
            "oi_reasoning": "Call-side OI positioning favors upside momentum expansion.",
            "greeks_reasoning": "Delta profile remains favorable for directional continuation.",
            "price_action_reasoning": "Price action confirms a momentum continuation structure.",
        }
        msg = tn._format_html(payload)
        assert "\U0001F9E0 <b>AI Factors</b>" in msg
        assert "• Institutional bias supports the bullish continuation setup." in msg
        assert "• Call-side OI positioning favors upside momentum expansion." in msg
        assert "• Delta profile remains favorable for directional continuation." in msg
        assert "• Price action confirms a momentum continuation structure." in msg

    def test_reasoning_details_takes_priority_over_top_level_keys_when_both_present(self):
        payload = {
            "symbol": "NIFTY", "signal_type": "BUY CE", "reasoning_details": ["Preferred source bullet"],
            "institutional_reasoning": "Should not appear",
        }
        msg = tn._format_html(payload)
        assert "• Preferred source bullet" in msg
        assert "Should not appear" not in msg

    def test_top_level_reasoning_keys_filter_none_and_empty_strings(self):
        payload = {
            "symbol": "NIFTY", "signal_type": "BUY CE",
            "institutional_reasoning": "Real bullet",
            "oi_reasoning": "", "greeks_reasoning": None, "price_action_reasoning": None,
        }
        msg = tn._format_html(payload)
        assert "• Real bullet" in msg
        assert msg.count("•") == 1

    def test_demo_footer_when_demo_test_true(self):
        payload = dict(EXAMPLE_PAYLOAD, demo_test=True)
        msg = tn._format_html(payload)
        assert "⚠️ DEMO TEST — No trade executed" in msg
        assert "Educational purpose only" not in msg

    def test_production_footer_when_demo_test_absent(self):
        msg = tn._format_html(EXAMPLE_PAYLOAD)
        assert "⚠️ Educational purpose only" in msg
        assert "DEMO TEST" not in msg

    def test_buy_label_for_pe_signal(self):
        # ai_trading_engine.py only ever produces "BUY CE" or "BUY PE" --
        # never a SELL/short recommendation -- so a PE signal must still
        # read "BUY ... PE", not "SELL ... PE" (the bug this test used to
        # lock in: the payload's own signal_type already says BUY_PE, and
        # the rendered message must match it, not invert it).
        payload = dict(EXAMPLE_PAYLOAD, signal_type="BUY_PE", entry_zone={"strike": 24500, "price": 90})
        msg = tn._format_html(payload)
        assert "BUY <b>NIFTY 24500 PE</b>" in msg
        assert "Above: <b>90</b>" in msg
        assert "SELL" not in msg

    def test_renders_the_snapshot_timestamp_when_present(self):
        # MARKET_SNAPSHOT_INTEGRITY_AUDIT.md: without this, a signal from
        # 3 minutes ago and one from 3 hours ago render identically.
        payload = dict(EXAMPLE_PAYLOAD, as_of_ts="2026-08-24T09:15:30")
        msg = tn._format_html(payload)
        assert "As of: <b>2026-08-24T09:15:30</b>" in msg

    def test_shows_unknown_rather_than_omitting_when_timestamp_is_missing(self):
        # EXAMPLE_PAYLOAD itself has no as_of_ts -- an old/untimestamped
        # signal must never silently look the same as a labeled one.
        msg = tn._format_html(EXAMPLE_PAYLOAD)
        assert "As of: <b>unknown</b>" in msg

    def test_buy_label_for_ce_signal(self):
        payload = dict(EXAMPLE_PAYLOAD, signal_type="BUY_CE", entry_zone={"strike": 24500, "price": 90})
        msg = tn._format_html(payload)
        assert "BUY <b>NIFTY 24500 CE</b>" in msg
        assert "Above: <b>90</b>" in msg

    def test_expiry_shows_unknown_and_contract_line_omitted_when_absent(self):
        # A payload from an older/bypassing caller that never supplied
        # expiry_date/trading_symbol -- must degrade honestly (an
        # unlabeled "?" expiry, not a fabricated date), not crash, and
        # never render an empty "Contract: <b></b>" line.
        payload = {
            "symbol": "NIFTY", "signal_type": "BUY_CE", "overall_bias": "BULLISH", "confidence": 80,
            "entry_zone": {"strike": 24500, "price": 90}, "targets": [100], "stop_loss": 80,
        }
        msg = tn._format_html(payload)
        assert "Expiry: <b>?</b>" in msg
        assert "Contract:" not in msg

    def test_fmt_expiry_falls_back_to_raw_string_on_unparseable_date(self):
        assert tn._fmt_expiry("not-a-date") == "not-a-date"
        assert tn._fmt_expiry(None) == "?"
        assert tn._fmt_expiry("2026-08-06") == "06-Aug-2026"


STRUCTURE_PAYLOAD = {
    "symbol": "NATURALGAS", "level": 5.20, "previous_role": "RESISTANCE", "current_role": "SUPPORT",
    "confidence": 86, "state": "BULLISH_RETEST_ACTIVE", "major_support": 5.20, "next_resistance": [5.35, 5.55],
}


class TestFormatStructureUpdate:
    def test_includes_the_role_flip_and_all_supplied_fields(self):
        msg = tn._format_structure_update(STRUCTURE_PAYLOAD)
        assert "⚠️ <b>STRUCTURE UPDATE</b>" in msg
        assert "NATURALGAS" in msg
        assert "5.2 RESISTANCE → SUPPORT" in msg
        assert "Composite confidence: 86%" in msg
        assert "Major support: 5.2" in msg
        assert "Next resistance: 5.35 / 5.55" in msg
        assert "State: BULLISH_RETEST_ACTIVE" in msg

    def test_omits_optional_sections_when_not_supplied(self):
        minimal = {"symbol": "NIFTY", "level": 24500, "previous_role": "SUPPORT", "current_role": "RESISTANCE"}
        msg = tn._format_structure_update(minimal)
        assert "Composite confidence" not in msg
        assert "Major support" not in msg
        assert "Next resistance" not in msg
        assert "State:" not in msg

    def test_non_flip_state_shows_level_and_state_not_a_fake_flip(self):
        # BREAKOUT_WATCH/REVERSAL_RISK aren't role flips -- no
        # previous_role/current_role at all.
        payload = {"symbol": "BANKNIFTY", "level": 56050, "state": "BREAKOUT_WATCH", "confidence": 72}
        msg = tn._format_structure_update(payload)
        assert "Level: 56050" in msg
        assert "→" not in msg
        assert "State: BREAKOUT_WATCH" in msg
        assert "Composite confidence: 72%" in msg

    def test_equal_roles_does_not_render_a_flip_line(self):
        payload = {"symbol": "GOLD", "level": 155000, "previous_role": "RESISTANCE", "current_role": "RESISTANCE"}
        msg = tn._format_structure_update(payload)
        assert "→" not in msg
        assert "Level: 155000" in msg

    def test_never_labeled_as_a_trade_signal(self):
        msg = tn._format_structure_update(STRUCTURE_PAYLOAD)
        assert "Suggested Trade" not in msg
        assert "Buy @" not in msg
        assert "Stop Loss" not in msg


class TestFormatStructureUpdateOverlay:
    def test_bullish_overlay_renders_buy_above(self):
        payload = dict(STRUCTURE_PAYLOAD, overlay={"direction": "BULLISH", "entry": 78040, "sl": 77910, "t1": 78170, "t2": 78300},
                        reversal_support=77920, reversal_resistance=78080, timeframe="3m + 5m confirmed")
        msg = tn._format_structure_update(payload)
        assert "📍 <b>Trade Plan Overlay</b>" in msg
        assert "Buy Above: 78040" in msg
        assert "SL: 77910" in msg
        assert "T1: 78170" in msg
        assert "T2: 78300" in msg
        assert "🔄 Reversal Support: 77920" in msg
        assert "🔄 Reversal Resistance: 78080" in msg
        assert "⏰ TF: 3m + 5m confirmed" in msg
        assert "⚠️ Informational structure overlay only — not an executed trade signal." in msg

    def test_bearish_overlay_renders_buy_pe_below(self):
        payload = dict(STRUCTURE_PAYLOAD, overlay={"direction": "BEARISH", "entry": 269.55, "sl": 270.25, "t1": 268.85, "t2": 268.15})
        msg = tn._format_structure_update(payload)
        assert "Buy PE Below: 269.55" in msg
        assert "Buy Above" not in msg

    def test_no_overlay_section_when_absent(self):
        msg = tn._format_structure_update(STRUCTURE_PAYLOAD)
        assert "Trade Plan Overlay" not in msg
        assert "not an executed trade signal" not in msg

    def test_option_strike_renders_when_present(self):
        payload = dict(
            STRUCTURE_PAYLOAD,
            overlay={"direction": "BULLISH", "entry": 78040, "sl": 77910, "t1": 78170, "t2": 78300,
                     "option_strike": {"strike": 78000, "option_type": "CE", "premium": 145.5}},
        )
        msg = tn._format_structure_update(payload)
        assert "Option: 78000 CE @ 145.5" in msg

    def test_option_strike_line_omitted_when_absent(self):
        payload = dict(STRUCTURE_PAYLOAD, overlay={"direction": "BULLISH", "entry": 78040, "sl": 77910, "t1": 78170, "t2": 78300})
        msg = tn._format_structure_update(payload)
        assert "Option:" not in msg

    def test_existing_plain_fields_unchanged_when_overlay_absent(self):
        # The pre-existing format (Composite confidence:/State:) must
        # stay byte-for-byte the same when there's no overlay -- this
        # addition is purely additive, never a re-skin of the base case.
        msg = tn._format_structure_update(STRUCTURE_PAYLOAD)
        assert "Composite confidence: 86%" in msg
        assert "State: BULLISH_RETEST_ACTIVE" in msg


class TestSendStructureUpdate:
    def test_returns_false_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "")
        assert tn.send_structure_update(STRUCTURE_PAYLOAD) is False

    def test_sends_and_dedups_independently_from_trading_signals(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: calls.append(1) or _FakeResponse())

        # A real trading signal for the SAME symbol must not interfere
        # with the structure update's own dedup state, and vice versa.
        assert tn.send_trading_intelligence_signal(EXAMPLE_PAYLOAD) is True
        assert tn.send_structure_update(STRUCTURE_PAYLOAD) is True
        assert len(calls) == 2

        # Repeating the SAME structure update is suppressed independently.
        assert tn.send_structure_update(STRUCTURE_PAYLOAD) is False
        assert len(calls) == 2

    def test_a_different_role_for_the_same_level_is_not_suppressed(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: calls.append(1) or _FakeResponse())

        assert tn.send_structure_update(STRUCTURE_PAYLOAD) is True
        flipped_back = dict(STRUCTURE_PAYLOAD, previous_role="SUPPORT", current_role="RESISTANCE")
        assert tn.send_structure_update(flipped_back) is True
        assert len(calls) == 2


GUARDIAN_PAYLOAD = {
    "position_id": "NATURALGAS_250_CE_2026-08-17T09:00:00", "symbol": "NATURALGAS", "strike": 250,
    "direction": "CE", "entry_price": 9.20, "current_premium": 9.55, "original_sl": 6.50,
    "smart_sl": 6.50, "original_target": 15.0, "smart_target_low": 11.0, "smart_target_high": 12.0,
    "breakout_target": None, "trade_health_score": 62.0, "trade_health_tier": "CAUTION",
    "action": "HOLD WITH CAUTION", "reason": "260 CE OI wall remains strong; no breakout confirmation.",
}


class TestSendTradeGuardianUpdate:
    def test_returns_false_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "")
        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is False

    def test_sends_and_shows_both_original_and_smart_values(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []

        def fake_post(url, json, timeout):
            calls.append((url, json, timeout))
            return _FakeResponse()

        monkeypatch.setattr(tn.requests, "post", fake_post)

        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is True
        text = calls[0][1]["text"]
        assert "6.5" in text  # both original SL and smart SL render (equal in this payload)
        assert "15" in text  # original target
        assert "11" in text  # smart target
        assert "HOLD WITH CAUTION" in text
        assert "Shadow/advisory only" in text

    def test_never_raises_when_post_fails(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")

        def raising_post(*a, **kw):
            raise ConnectionError("boom")

        monkeypatch.setattr(tn.requests, "post", raising_post)
        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is False

    def test_duplicate_within_window_is_suppressed(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda url, json, timeout: calls.append(1) or _FakeResponse())

        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is True
        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is False  # same fingerprint, within window
        assert len(calls) == 1

    def test_changed_action_is_not_suppressed(self, monkeypatch):
        monkeypatch.setattr(tn, "TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setattr(tn, "TELEGRAM_SIGNALS_CHANNEL_ID", "-1003927831776")
        calls = []
        monkeypatch.setattr(tn.requests, "post", lambda url, json, timeout: calls.append(1) or _FakeResponse())

        assert tn.send_trade_guardian_update(GUARDIAN_PAYLOAD) is True
        changed = dict(GUARDIAN_PAYLOAD, action="TRAIL", smart_sl=9.20)
        assert tn.send_trade_guardian_update(changed) is True
        assert len(calls) == 2
