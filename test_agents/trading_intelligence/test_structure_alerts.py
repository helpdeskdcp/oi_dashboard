"""
test_agents/trading_intelligence/test_structure_alerts.py -- Milestone 20,
Phase 2: regression tests for agents/trading_intelligence/structure_alerts.py,
the STRUCTURE-ALERT-ONLY wiring of institutional_levels.py into the live
Trading Intelligence cycle. Pure unit tests -- no DB, no real HTTP/candle
fetch (everything monkeypatched), matching this package's own convention
of never importing app.py from test_agents/.
"""
import datetime as dt
import glob
import os
import types

import pytest

from agents import config
from agents.runtime import market_session
from agents.trading_intelligence import data_access, market_data, structure_alerts as sa, telegram_notifier
from agents.trading_intelligence import structure_chart


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    sa._last_alert_by_key.clear()
    sa._last_preview_by_symbol.clear()
    monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", True)
    yield
    sa._last_alert_by_key.clear()
    sa._last_preview_by_symbol.clear()


@pytest.fixture(autouse=True)
def _no_real_chart_files():
    """These tests don't mock structure_chart.render_structure_chart --
    a real (small, fast) JPEG genuinely gets written to disk when a
    test's candle data produces a real reversal, which is legitimate
    end-to-end verification (see test_a_real_reversal_renders_and_attaches_a_chart
    below). This fixture just deletes whatever this test process wrote,
    so a full test run never leaves real files behind in the repo's own
    static/structure_charts/ directory."""
    pattern = os.path.join(structure_chart.CHART_DIR, "**", "*.jpg")
    before = set(glob.glob(pattern, recursive=True))
    yield
    after = set(glob.glob(pattern, recursive=True))
    for f in after - before:
        os.remove(f)


def _snapshot(*, available=True, reason=None, strikes=None, atm=100, underlying=103, vwap=100):
    return types.SimpleNamespace(available=available, reason=reason, strikes=strikes or [],
                                  atm=atm, underlying_ltp=underlying, vwap=vwap)


def _candle(minute, o, h, l, c, v=1000):
    base = dt.datetime(2026, 8, 10, 9, 0)
    return {"datetime": base + dt.timedelta(minutes=minute), "open": o, "high": h, "low": l, "close": c, "volume": v}


LEVEL = 24500  # NIFTY's real profile (breakout_buffer=20, retest_tolerance=5) --
# scale the candle data to actually clear those real thresholds.
REVERSAL_CANDLES = [
    _candle(0, 24480, 24540, 24470, 24535),   # close 24535 > 24500+20
    _candle(3, 24530, 24545, 24503, 24540),   # retest low 24503 <= 24505, big lower wick, close above
]


def _mock_single_level(monkeypatch, *, weight=0.9):
    """weighted_levels() clustering itself is institutional_levels.py's
    own tested responsibility (test_institutional_levels.py) -- these
    orchestration tests fix its output to one deterministic level so
    they only exercise structure_alerts.py's own gating/dedup/wiring
    logic, not the full clustering pipeline."""
    monkeypatch.setattr(sa.il, "weighted_levels",
                         lambda *a, **kw: [{"level": LEVEL, "type": "RESISTANCE", "weight": weight, "sources": ["TEST"]}])


class TestEvaluateSymbolGating:
    def test_returns_not_evaluated_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        result = sa.evaluate_symbol("NIFTY")
        assert result == {"symbol": "NIFTY", "evaluated": False, "reason": "TI_ENABLE_STRUCTURE_ALERTS is False"}

    def test_returns_not_evaluated_when_market_closed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (False, "Weekend"))
        result = sa.evaluate_symbol("NIFTY")
        assert result["evaluated"] is False
        assert "market closed" in result["reason"]

    def test_returns_not_evaluated_when_snapshot_unavailable(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(available=False, reason="no cycle logged"))
        assert result["evaluated"] is False
        assert "no OI snapshot" in result["reason"]

    def test_returns_not_evaluated_when_no_candle_data(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(), candles=[])
        assert result["evaluated"] is False
        assert "no candle data" in result["reason"]

    def test_never_fetches_candles_or_snapshot_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        calls = []
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: calls.append("snapshot"))
        monkeypatch.setattr(data_access, "load_candles", lambda *a, **kw: calls.append("candles"))
        sa.evaluate_symbol("NIFTY")
        assert calls == []


class TestEvaluateSymbolAlerting:
    def test_sends_a_structure_alert_for_a_real_reversal(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["evaluated"] is True
        assert len(sent) >= 1
        alert = sent[0]
        assert alert["symbol"] == "NIFTY"
        assert alert["current_role"] == "SUPPORT"

    def test_never_calls_the_trade_signal_sender(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        signal_calls = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda p: signal_calls.append(p) or True)
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: True)

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert signal_calls == []

    def test_duplicate_alert_within_15_minutes_is_suppressed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        now = dt.datetime(2026, 8, 10, 12, 0)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=now)
        first_count = len(sent)
        assert first_count >= 1

        later = now + dt.timedelta(minutes=10)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=later)
        assert len(sent) == first_count  # no new send -- still within the 15-minute window

    def test_alert_after_15_minutes_is_not_suppressed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        now = dt.datetime(2026, 8, 10, 12, 0)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=now)
        first_count = len(sent)

        later = now + dt.timedelta(minutes=16)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=later)
        assert len(sent) > first_count

    def test_a_flat_uninteresting_level_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)
        flat_candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(60)]

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=100, vwap=100), candles=flat_candles)

        assert sent == []


class TestChartWiring:
    def test_a_real_reversal_renders_and_attaches_a_chart(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        received_chart_paths = []
        monkeypatch.setattr(
            telegram_notifier, "send_structure_update",
            lambda payload, chart_path=None, **kw: received_chart_paths.append(chart_path) or True,
        )

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert len(received_chart_paths) == 1
        chart_path = received_chart_paths[0]
        assert chart_path is not None
        assert os.path.exists(chart_path)
        assert chart_path.endswith(".jpg")

    def test_a_chart_rendering_failure_never_blocks_the_text_alert(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        monkeypatch.setattr(sa.structure_chart, "render_structure_chart", lambda *a, **kw: None)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["evaluated"] is True
        assert len(sent) == 1


class TestTradePlanOverlayWiring:
    """Milestone 20, Phase 3: the overlay is derived-and-attached data
    only -- these tests confirm it rides along on the SAME
    send_structure_update() payload/dedup path already proven never to
    touch paper_trading/ai_trading_engine, rather than opening any new
    code path of its own."""

    def test_overlay_is_attached_for_a_high_confidence_reversal(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert len(sent) == 1
        overlay = sent[0]["overlay"]
        # entry = breakout_high(24540) + NIFTY buffer(5) = 24545; sl = retest_low(24503) - 5 = 24498
        assert overlay == {"direction": "BULLISH", "entry": 24545, "sl": 24498, "t1": 24592, "t2": 24639}

    def test_reversal_support_and_resistance_come_from_other_weighted_levels(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [
            {"level": LEVEL, "type": "RESISTANCE", "weight": 0.9, "sources": ["TEST"]},
            {"level": LEVEL - 100, "type": "SUPPORT", "weight": 0.7, "sources": ["TEST"]},
            {"level": LEVEL + 150, "type": "RESISTANCE", "weight": 0.7, "sources": ["TEST"]},
        ])
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert sent[0]["reversal_support"] == LEVEL - 100
        assert sent[0]["reversal_resistance"] == LEVEL + 150

    def test_no_overlay_key_when_confidence_is_too_low(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        monkeypatch.setattr(sa.il, "compute_trade_plan_overlay", lambda *a, **kw: None)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: sent.append(payload) or True)

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert "overlay" not in sent[0]

    def test_overlay_never_creates_a_paper_trade_or_calls_paper_trading(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: True)
        from agents.trading_intelligence import paper_trading
        pt_calls = []
        monkeypatch.setattr(paper_trading, "enter_from_recommendation", lambda *a, **kw: pt_calls.append(1))

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert pt_calls == []


class TestPreviewGeneration:
    """Milestone 20, Phase 4: preview-only charts for a symbol whose
    best candidate level hasn't cleared the live threshold (the real
    CRUDEOIL case) -- NEVER sent to Telegram, NEVER touching real-alert
    dedup state."""

    def _below_threshold_candidate(self):
        return {"level": 8000.0, "type": "RESISTANCE", "weight": 0.35, "sources": ["OI_WALL_CE", "ROUND_NUMBER"], "is_major": False}

    def test_preview_generated_when_nothing_alertable_fires(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [])  # nothing major -> no real alert loop iterations
        monkeypatch.setattr(sa.il, "best_candidate_level", lambda *a, **kw: self._below_threshold_candidate())
        rendered = []
        monkeypatch.setattr(
            sa.structure_chart, "render_structure_chart",
            lambda *a, **kw: rendered.append(kw) or "static/structure_charts/previews/CRUDEOIL_x.jpg",
        )

        result = sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert result["preview_chart"] == "static/structure_charts/previews/CRUDEOIL_x.jpg"
        assert len(rendered) == 1
        assert rendered[0]["preview"] is True

    def test_no_preview_when_a_real_alert_was_sent(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload, **kw: True)
        preview_calls = []
        monkeypatch.setattr(sa, "_maybe_render_preview", lambda *a, **kw: preview_calls.append(1))

        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["preview_chart"] is None
        assert preview_calls == []

    def test_preview_never_calls_telegram(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(sa.il, "best_candidate_level", lambda *a, **kw: self._below_threshold_candidate())
        telegram_calls = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda *a, **kw: telegram_calls.append(1) or True)
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda *a, **kw: telegram_calls.append(1) or True)

        sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert telegram_calls == []

    def test_preview_does_not_touch_real_alert_dedup_state(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(sa.il, "best_candidate_level", lambda *a, **kw: self._below_threshold_candidate())
        monkeypatch.setattr(sa.structure_chart, "render_structure_chart", lambda *a, **kw: "x.jpg")

        sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert sa._last_alert_by_key == {}

    def test_preview_is_throttled_like_real_alerts(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(sa.il, "best_candidate_level", lambda *a, **kw: self._below_threshold_candidate())
        rendered = []
        monkeypatch.setattr(sa.structure_chart, "render_structure_chart", lambda *a, **kw: rendered.append(1) or "x.jpg")

        now = dt.datetime(2026, 8, 10, 12, 0)
        sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES, now=now)
        assert len(rendered) == 1

        later = now + dt.timedelta(minutes=5)
        sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES, now=later)
        assert len(rendered) == 1  # still throttled

        much_later = now + dt.timedelta(minutes=16)
        sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES, now=much_later)
        assert len(rendered) == 2

    def test_returns_none_when_there_is_no_candidate_at_all(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(sa.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(sa.il, "best_candidate_level", lambda *a, **kw: None)

        result = sa.evaluate_symbol("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert result["preview_chart"] is None


class TestRunStructureAlertCycle:
    def test_returns_empty_dict_immediately_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        calls = []
        monkeypatch.setattr(market_session, "active_symbols", lambda symbols, **kw: calls.append(1) or [])
        assert sa.run_structure_alert_cycle() == {}
        assert calls == []

    def test_evaluates_only_the_given_symbols(self, monkeypatch):
        seen = []
        monkeypatch.setattr(sa, "evaluate_symbol", lambda sym, **kw: seen.append(sym) or {"symbol": sym, "evaluated": False, "reason": "x"})
        sa.run_structure_alert_cycle(symbols=["CRUDEOIL", "GOLD"])
        assert seen == ["CRUDEOIL", "GOLD"]

    def test_defaults_to_active_symbols_when_none_given(self, monkeypatch):
        monkeypatch.setattr(market_session, "active_symbols", lambda symbols, **kw: ["NATURALGAS"])
        seen = []
        monkeypatch.setattr(sa, "evaluate_symbol", lambda sym, **kw: seen.append(sym) or {"symbol": sym, "evaluated": False, "reason": "x"})
        sa.run_structure_alert_cycle()
        assert seen == ["NATURALGAS"]
