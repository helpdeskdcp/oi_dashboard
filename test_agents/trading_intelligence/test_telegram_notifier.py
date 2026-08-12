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
    yield
    tn._last_sent_by_fingerprint.clear()


class _FakeResponse:
    def raise_for_status(self):
        pass


EXAMPLE_PAYLOAD = {
    "symbol": "NIFTY", "signal_type": "BUY_CE", "overall_bias": "BULLISH", "confidence": 82,
    "entry_zone": {"strike": 24900, "price": 118}, "targets": [132, 148, 166], "stop_loss": 106,
    "institutional_score": 78, "premium_momentum": "STRONG", "oi_structure": "BULLISH",
    "vwap_structure": "ABOVE", "repeated_rejection": True,
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
        assert "BUY 24900 CE ABOVE <b>118</b>" in msg
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

    def test_sell_label_for_pe_signal(self):
        payload = dict(EXAMPLE_PAYLOAD, signal_type="BUY_PE", entry_zone={"strike": 24500, "price": 90})
        msg = tn._format_html(payload)
        assert "SELL 24500 PE ABOVE <b>90</b>" in msg
