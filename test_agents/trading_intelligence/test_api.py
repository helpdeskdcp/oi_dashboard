import datetime as dt
import types

from agents.trading_intelligence import ai_trading_engine
from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import execution_state
from agents.trading_intelligence import institutional_intelligence, market_data, paper_trading, telegram_notifier
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_realistic_chain


def _make_recommendation(**overrides):
    base = dict(
        symbol="NIFTY", action="BUY CE", direction="CE", strike=24900, market_bias="BULLISH",
        confidence=82, probability=0.6, probability_note="calibrated", risk_score=20,
        entry_price=118.0, sl_price=106.0, target_price=132.0, targets=[132.0, 148.0, 166.0],
        expected_move_pts=50.0, time_horizon="intraday", qty=50, reasoning="Strong bullish setup",
        institutional_reasoning="Institutional buying detected at 24900 CE",
        oi_reasoning="Fresh long buildup, OI supports bulls",
        greeks_reasoning="Delta/Gamma favorable",
        price_action_reasoning="Repeated rejection at support, price holding above VWAP",
    )
    base.update(overrides)
    return ai_trading_engine.Recommendation(**base)


class TestGetSymbolOverview:
    def test_unavailable_symbol(self, ti_db):
        result = ti_api.get_symbol_overview("NOT_A_REAL_SYMBOL")
        assert result["available"] is False

    def test_available_symbol_has_every_documented_section(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert result["available"] is True
        assert set(result.keys()) >= {
            "symbol", "available", "market_data", "institutional_intelligence",
            "strikes", "recommendation", "timeframes",
        }

    def test_everything_is_json_serializable(self, ti_db):
        import json
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        json.dumps(result, default=str)  # must not raise

    def test_snapshot_and_analysis_are_each_computed_exactly_once(self, ti_db, monkeypatch):
        """The Priority-1 review's whole point: get_symbol_overview() must
        thread its ONE snapshot/findings read through institutional_intelligence
        and ai_trading_engine, never re-fetching the cycle or re-running the
        full institutional sweep a second (or third) time per call."""
        insert_realistic_chain(ti_db, symbol="NIFTY")

        snapshot_calls, analyze_calls = [], []
        real_get_snapshot = market_data.get_snapshot
        real_analyze = institutional_intelligence.analyze

        def counting_get_snapshot(*args, **kwargs):
            snapshot_calls.append(1)
            return real_get_snapshot(*args, **kwargs)

        def counting_analyze(*args, **kwargs):
            analyze_calls.append(1)
            return real_analyze(*args, **kwargs)

        monkeypatch.setattr(market_data, "get_snapshot", counting_get_snapshot)
        monkeypatch.setattr(institutional_intelligence, "analyze", counting_analyze)

        ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))

        assert len(snapshot_calls) == 1
        assert len(analyze_calls) == 1


class TestGetPaperTradingSummary:
    def test_reflects_open_and_closed_trades(self, ti_db):
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=130.0, sl_price=85.0, qty=50)
        summary = ti_api.get_paper_trading_summary()
        assert len(summary["open_trades"]) == 1
        assert summary["stats"]["total_trades"] == 0


class TestGetOverview:
    def test_returns_every_watched_symbol(self, ti_db):
        from agents import config
        insert_realistic_chain(ti_db, symbol="NIFTY")
        overview = ti_api.get_overview()
        assert set(overview["symbols"].keys()) == set(config.TI_WATCHED_SYMBOLS)

    def test_includes_agent_health_and_paper_trading(self, ti_db):
        overview = ti_api.get_overview()
        assert "agent_health" in overview
        assert "paper_trading" in overview

    def test_fully_json_serializable(self, ti_db):
        import json
        insert_realistic_chain(ti_db, symbol="NIFTY")
        overview = ti_api.get_overview()
        json.dumps(overview, default=str)


class TestBuildTelegramPayload:
    """Milestone 19: _build_telegram_payload() maps a real Recommendation
    onto telegram_notifier's payload shape -- never inventing
    institutional_score/premium_momentum/oi_structure/vwap_structure/
    repeated_rejection, since this engine doesn't compute those as
    discrete fields (see telegram_notifier.py's own docstring)."""

    def test_maps_real_recommendation_fields(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)

        assert payload["symbol"] == "NIFTY"
        assert payload["signal_type"] == "BUY_CE"
        assert payload["overall_bias"] == "BULLISH"
        assert payload["confidence"] == 82
        assert payload["entry_zone"] == {"strike": 24900, "price": 118.0}
        assert payload["targets"] == [132.0, 148.0, 166.0]
        assert payload["stop_loss"] == 106.0
        assert payload["reasoning"] == "Strong bullish setup"

    def test_never_fabricates_structured_ai_factor_fields(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)
        for key in ("institutional_score", "premium_momentum", "oi_structure", "vwap_structure", "repeated_rejection"):
            assert key not in payload

    def test_passes_the_real_reasoning_strings_through_as_details(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)
        assert payload["reasoning_details"] == [
            "Institutional buying detected at 24900 CE",
            "Fresh long buildup, OI supports bulls",
            "Delta/Gamma favorable",
            "Repeated rejection at support, price holding above VWAP",
        ]

    def test_omits_empty_reasoning_strings(self):
        rec = _make_recommendation(oi_reasoning="", greeks_reasoning="")
        payload = ti_api._build_telegram_payload(rec)
        assert payload["reasoning_details"] == [
            "Institutional buying detected at 24900 CE",
            "Repeated rejection at support, price holding above VWAP",
        ]


class TestRunScheduledCycleTelegramGate:
    """Milestone 19: run_scheduled_cycle() must call
    telegram_notifier.send_trading_intelligence_signal() exactly once per
    symbol whose Recommendation is an actionable BUY at/above
    config.TI_TELEGRAM_MIN_CONFIDENCE, and never otherwise -- e.g. never
    for HOLD/NO_TRADE, never for a below-threshold BUY, and never merely
    because get_overview()/get_symbol_overview() (a dashboard read) ran."""

    def _wire_single_symbol_cycle(self, monkeypatch, *, rec, ti_db):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY",))
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: types.SimpleNamespace(available=True))
        monkeypatch.setattr(institutional_intelligence, "analyze", lambda *a, **kw: {"available": True, "findings": []})
        monkeypatch.setattr(ai_trading_engine, "evaluate", lambda *a, **kw: rec)
        monkeypatch.setattr(paper_trading, "enter_from_recommendation", lambda *a, **kw: 1)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: sent.append(payload) or True)
        return sent

    def test_sends_for_a_high_confidence_actionable_buy(self, ti_db, monkeypatch):
        rec = _make_recommendation(confidence=82)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert len(sent) == 1
        assert sent[0]["symbol"] == "NIFTY"
        assert sent[0]["signal_type"] == "BUY_CE"

    def test_does_not_send_below_the_confidence_threshold(self, ti_db, monkeypatch):
        rec = _make_recommendation(confidence=50)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_does_not_send_for_hold(self, ti_db, monkeypatch):
        rec = _make_recommendation(action="HOLD", direction=None, confidence=90)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_does_not_send_for_no_trade(self, ti_db, monkeypatch):
        rec = _make_recommendation(action="NO_TRADE", direction=None, confidence=None)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_a_plain_dashboard_overview_read_never_sends(self, ti_db, monkeypatch):
        """get_symbol_overview()/get_overview() (a manual dashboard load)
        must never trigger a Telegram signal -- only run_scheduled_cycle()
        (the autonomous/manual-run-cycle path) does. Uses REAL chain data
        (not the stubbed evaluate() the other tests in this class use) so
        this genuinely exercises get_overview()'s real code path end to
        end, confirming the absence of any notifier call isn't just an
        artifact of a mocked evaluate()."""
        insert_realistic_chain(ti_db, symbol="NIFTY")
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: sent.append(payload) or True)

        ti_api.get_overview()

        assert sent == []


class TestRunScheduledCycleSymbolsParam:
    """Milestone 19+: run_scheduled_cycle(symbols=...) lets a caller
    restrict one call to a subset of config.TI_WATCHED_SYMBOLS -- what
    agent_runtime._trading_intelligence_cycle() now passes (the
    currently-exchange-open subset) so an NSE symbol's stale post-close
    cycle data is never evaluated during MCX-only hours."""

    def test_defaults_to_every_watched_symbol_when_omitted(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY", "CRUDEOIL"))
        seen = []
        monkeypatch.setattr(market_data, "get_snapshot", lambda symbol, **kw: seen.append(symbol) or types.SimpleNamespace(available=False, reason="no data"))

        ti_api.run_scheduled_cycle()

        assert seen == ["NIFTY", "CRUDEOIL"]

    def test_processes_only_the_given_symbols(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY", "BANKNIFTY", "CRUDEOIL"))
        seen = []
        monkeypatch.setattr(market_data, "get_snapshot", lambda symbol, **kw: seen.append(symbol) or types.SimpleNamespace(available=False, reason="no data"))

        results = ti_api.run_scheduled_cycle(symbols=["CRUDEOIL"])

        assert seen == ["CRUDEOIL"]
        assert set(results.keys()) == {"CRUDEOIL"}


class TestRunScheduledCycleExecutionStateShadow:
    """Post-launch upgrade, Phase B1/B2: run_scheduled_cycle() must create
    exactly one execution_state record (execution_id ==
    f"paper_trade_{trade_id}") and advance it SIGNAL -> APPROVED -> READY
    -> ORDER_INTENT -> SUBMITTED -> FILLED -> MONITORING whenever it
    actually opens a real paper trade this cycle -- gated off by default
    (config.TI_ENABLE_EXECUTION_STATE_SHADOW), and never for a cycle that
    doesn't open a trade (HOLD/NO_TRADE, or an unavailable snapshot) even
    when the flag is on. A failure in this wiring must never break the
    real cycle it's observing."""

    def _wire_single_symbol_cycle(self, monkeypatch, *, rec, ti_db, trade_id=1):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY",))
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: types.SimpleNamespace(available=True))
        monkeypatch.setattr(institutional_intelligence, "analyze", lambda *a, **kw: {"available": True, "findings": []})
        monkeypatch.setattr(ai_trading_engine, "evaluate", lambda *a, **kw: rec)
        monkeypatch.setattr(paper_trading, "enter_from_recommendation", lambda *a, **kw: trade_id)
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: True)

    def test_flag_off_creates_no_execution_state_row_even_for_an_actionable_trade(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", False)
        rec = _make_recommendation(confidence=82)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert execution_state.list_executions() == []

    def test_flag_on_creates_and_advances_execution_to_monitoring_for_an_actionable_trade(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        rec = _make_recommendation(confidence=82)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db, trade_id=7)

        ti_api.run_scheduled_cycle()

        row = execution_state.get_execution("paper_trade_7")
        assert row is not None
        assert row["current_state"] == "MONITORING"
        transitions = [t["to_state"] for t in execution_state.recent_transitions("paper_trade_7")]
        assert transitions[::-1] == ["SIGNAL", "APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]
        assert row["instrument"] == "NIFTY"
        assert row["direction"] == "CE"
        assert row["strike"] == 24900
        assert row["entry_price"] == 118.0
        assert row["sl"] == 106.0
        assert row["t1"] == 132.0
        assert row["t2"] == 148.0
        assert row["t3"] == 166.0
        assert row["confidence"] == 82
        assert row["signal_reference"] == "ti_paper_trades:7"

    def test_flag_on_creates_no_row_for_hold(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        rec = _make_recommendation(action="HOLD", direction=None, confidence=90)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db, trade_id=None)

        ti_api.run_scheduled_cycle()

        assert execution_state.list_executions() == []

    def test_flag_on_creates_no_row_for_no_trade(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        rec = _make_recommendation(action="NO_TRADE", direction=None, confidence=None)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db, trade_id=None)

        ti_api.run_scheduled_cycle()

        assert execution_state.list_executions() == []

    def test_flag_on_is_idempotent_across_repeated_cycles_for_the_same_trade_id(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        rec = _make_recommendation(confidence=82)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db, trade_id=3)

        ti_api.run_scheduled_cycle()
        ti_api.run_scheduled_cycle()

        assert len(execution_state.list_executions()) == 1
        assert execution_state.get_execution("paper_trade_3")["current_state"] == "MONITORING"

    def test_a_bug_in_execution_state_wiring_never_breaks_the_real_cycle(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        rec = _make_recommendation(confidence=82)
        self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db, trade_id=9)
        monkeypatch.setattr(
            execution_state, "create_execution",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        results = ti_api.run_scheduled_cycle()

        assert results["NIFTY"]["trade_opened"] is True
        assert results["NIFTY"]["trade_id"] == 9
        assert execution_state.list_executions() == []


class TestRunScheduledCycleExecutionStateShadowExitSide:
    """Post-launch upgrade, Phase B2 (exit side): run_scheduled_cycle()
    must advance an already-MONITORING execution_state record through
    EXIT_INTENT -> EXIT -> COMPLETED exactly when ai_trading_engine.
    evaluate() closes the matching paper trade this cycle (target/SL
    hit) -- detected via an open_trades-before/after diff around the
    evaluate() call, never by re-implementing evaluate()'s own close
    logic. Gated off by default; a trade closing with no matching
    execution_state row (shadow was off when it opened) must be a
    graceful no-op, never a crash."""

    def _open_trade_and_track_to_monitoring(self, *, symbol="NIFTY"):
        trade_id = ts.open_trade(
            symbol=symbol, strike=24900, direction="CE", entry_price=118.0,
            target_price=132.0, sl_price=106.0, qty=50, confidence=82,
        )
        execution_id = f"paper_trade_{trade_id}"
        execution_state.create_execution(
            execution_id, instrument=symbol, direction="CE", strike=24900,
            entry_price=118.0, quantity=50, sl=106.0, t1=132.0, confidence=82,
            decision_reason="test setup", signal_reference=f"ti_paper_trades:{trade_id}",
        )
        for state in ("APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"):
            result = execution_state.transition(execution_id, state, reason="test setup")
            assert result["ok"], result
        return trade_id, execution_id

    def _wire_closing_cycle(self, monkeypatch, *, symbol, trade_id, exit_price, exit_reason):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", (symbol,))
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: types.SimpleNamespace(available=True))
        monkeypatch.setattr(institutional_intelligence, "analyze", lambda *a, **kw: {"available": True, "findings": []})

        def _evaluate_and_close(*a, **kw):
            ts.close_trade(trade_id, exit_price=exit_price, exit_reason=exit_reason)
            return _make_recommendation(action="NO_TRADE", direction=None, confidence=None)

        monkeypatch.setattr(ai_trading_engine, "evaluate", _evaluate_and_close)
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: True)

    def test_flag_on_advances_a_monitored_execution_to_completed_when_the_trade_closes(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_cycle(monkeypatch, symbol="NIFTY", trade_id=trade_id, exit_price=132.0, exit_reason="TARGET_HIT")

        ti_api.run_scheduled_cycle()

        row = execution_state.get_execution(execution_id)
        assert row["current_state"] == "COMPLETED"
        transitions = [t["to_state"] for t in execution_state.recent_transitions(execution_id) if t["accepted"]]
        assert transitions[:3] == ["COMPLETED", "EXIT", "EXIT_INTENT"]

    def test_flag_off_never_advances_a_closed_trades_execution(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", False)
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_cycle(monkeypatch, symbol="NIFTY", trade_id=trade_id, exit_price=132.0, exit_reason="TARGET_HIT")

        ti_api.run_scheduled_cycle()

        assert execution_state.get_execution(execution_id)["current_state"] == "MONITORING"

    def test_a_closed_trade_with_no_execution_state_row_is_a_graceful_no_op(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        # Opened directly (as if the shadow flag were off at the time) --
        # no execution_state row exists for this trade_id at all.
        trade_id = ts.open_trade(
            symbol="NIFTY", strike=24900, direction="CE", entry_price=118.0,
            target_price=132.0, sl_price=106.0, qty=50, confidence=82,
        )
        self._wire_closing_cycle(monkeypatch, symbol="NIFTY", trade_id=trade_id, exit_price=132.0, exit_reason="TARGET_HIT")

        results = ti_api.run_scheduled_cycle()

        assert execution_state.get_execution(f"paper_trade_{trade_id}") is None
        assert results["NIFTY"]["available"] is True

    def test_a_bug_in_exit_side_wiring_never_breaks_the_real_cycle(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_cycle(monkeypatch, symbol="NIFTY", trade_id=trade_id, exit_price=132.0, exit_reason="TARGET_HIT")
        monkeypatch.setattr(
            ts, "list_closed_trades",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        results = ti_api.run_scheduled_cycle()

        assert results["NIFTY"]["available"] is True
        assert execution_state.get_execution(execution_id)["current_state"] == "MONITORING"
