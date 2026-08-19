import datetime as dt
import types

import pytest

from agents.trading_intelligence import ai_trading_engine
from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import execution_state
from agents.trading_intelligence import institutional_intelligence, market_data, paper_trading, telegram_notifier
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_realistic_chain


@pytest.fixture(autouse=True)
def _reset_overview_cache():
    """get_overview()'s short-TTL cache (post-launch upgrade) is
    module-level state that otherwise leaks across tests within the
    same pytest process -- a cached result from one test's mocked
    market_data.get_snapshot() could be served, stale and wrong, to a
    completely different test. Reset before AND after so a test that
    itself asserts on cache behavior doesn't leave state for the next
    one either."""
    ti_api._overview_cache["ts"] = 0.0
    ti_api._overview_cache["data"] = None
    yield
    ti_api._overview_cache["ts"] = 0.0
    ti_api._overview_cache["data"] = None


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


class TestGetOverviewCacheAndConcurrency:
    """Post-launch upgrade: get_overview() latency fix -- short-TTL cache
    with stampede protection, plus per-symbol parallelism with failure
    isolation. Every test here monkeypatches _compute_overview() itself
    (a call-counting stub) rather than the real per-symbol machinery --
    these tests are about the CACHE/CONCURRENCY behavior, not about
    re-testing get_symbol_overview() itself (already covered above)."""

    def _counting_compute(self, monkeypatch, *, result_factory=None, sleep_seconds=0.0):
        calls = []

        def _fake(*, expiry_date, expiry_dates):
            calls.append(1)
            if sleep_seconds:
                import time
                time.sleep(sleep_seconds)
            if result_factory:
                return result_factory(len(calls))
            return {"symbols": {"NIFTY": {"available": True}}, "paper_trading": {}, "agent_health": {}, "call": len(calls)}

        monkeypatch.setattr(ti_api, "_compute_overview", _fake)
        return calls

    def test_cache_hit_within_ttl_never_recomputes(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_OVERVIEW_CACHE_TTL_SECONDS", 10)
        calls = self._counting_compute(monkeypatch)

        first = ti_api.get_overview()
        second = ti_api.get_overview()

        assert len(calls) == 1
        assert first == second
        assert first["call"] == 1

    def test_cache_miss_after_ttl_expires_recomputes(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_OVERVIEW_CACHE_TTL_SECONDS", 10)
        calls = self._counting_compute(monkeypatch)

        first = ti_api.get_overview()
        # Simulate TTL having elapsed without a real sleep -- directly
        # backdate the cache timestamp, the same effect as real time
        # passing, deterministic rather than timing-flaky.
        ti_api._overview_cache["ts"] -= 11
        second = ti_api.get_overview()

        assert len(calls) == 2
        assert first["call"] == 1
        assert second["call"] == 2

    def test_use_cache_false_always_recomputes(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_OVERVIEW_CACHE_TTL_SECONDS", 10)
        calls = self._counting_compute(monkeypatch)

        ti_api.get_overview()
        ti_api.get_overview(use_cache=False)
        ti_api.get_overview(use_cache=False)

        assert len(calls) == 3

    def test_overlapping_requests_do_not_each_trigger_their_own_recompute(self, monkeypatch):
        """Stampede protection: several callers arriving while the cache
        is empty and a recompute is already in flight must all get a
        real result, but only ONE genuine recompute should happen --
        not len(threads) separate full sweeps."""
        from agents import config
        monkeypatch.setattr(config, "TI_OVERVIEW_CACHE_TTL_SECONDS", 10)
        calls = self._counting_compute(monkeypatch, sleep_seconds=0.3)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: ti_api.get_overview(), range(8)))

        assert all(r is not None for r in results)
        assert len(calls) <= 2  # the in-flight one, plus at most one more that started just after it released

    def test_one_symbol_failure_is_isolated_from_the_others(self, ti_db, monkeypatch):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY", "BROKEN_SYMBOL"))
        real_get_symbol_overview = ti_api.get_symbol_overview

        def _flaky(symbol, **kw):
            if symbol == "BROKEN_SYMBOL":
                raise RuntimeError("simulated failure")
            return real_get_symbol_overview(symbol, **kw)

        monkeypatch.setattr(ti_api, "get_symbol_overview", _flaky)

        overview = ti_api.get_overview(use_cache=False)

        assert overview["symbols"]["NIFTY"]["available"] is True
        assert overview["symbols"]["BROKEN_SYMBOL"]["available"] is False
        assert "internal error" in overview["symbols"]["BROKEN_SYMBOL"]["reason"]

    def test_response_schema_and_symbol_order_unchanged(self, ti_db):
        from agents import config
        insert_realistic_chain(ti_db, symbol="NIFTY")

        overview = ti_api.get_overview(use_cache=False)

        assert list(overview["symbols"].keys()) == list(config.TI_WATCHED_SYMBOLS)
        assert set(overview.keys()) == {"symbols", "paper_trading", "agent_health"}


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


class TestGetSymbolOverviewExecutionStateReconciliation:
    """Reconciliation fix: ai_trading_engine.evaluate() can close an open
    paper trade from ANY caller, not only run_scheduled_cycle() --
    get_symbol_overview() (a plain dashboard read, fired by every
    /api/trading-intelligence/overview poll, including the client's own
    15s auto-refresh) calls evaluate() too. A real trade
    (paper_trade_75, 2026-08-18) was found CLOSED in ti_paper_trades
    while its execution_state row stayed stuck at MONITORING forever,
    because only run_scheduled_cycle() reconciled exits. Both call
    sites must share the exact same _reconcile_execution_state_exits()
    -- these tests exercise get_symbol_overview()'s own side of that."""

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

    def _wire_closing_read(self, monkeypatch, *, trade_id, exit_price, exit_reason):
        def _evaluate_and_close(*a, **kw):
            ts.close_trade(trade_id, exit_price=exit_price, exit_reason=exit_reason)
            return _make_recommendation(action="NO_TRADE", direction=None, confidence=None)

        monkeypatch.setattr(ai_trading_engine, "evaluate", _evaluate_and_close)

    def test_a_trade_closing_during_a_plain_dashboard_read_still_reaches_completed(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        insert_realistic_chain(ti_db, symbol="NIFTY")
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_read(monkeypatch, trade_id=trade_id, exit_price=132.0, exit_reason="TARGET HIT")

        ti_api.get_symbol_overview("NIFTY")

        row = execution_state.get_execution(execution_id)
        assert row["current_state"] == "COMPLETED"

    def test_flag_off_never_touches_execution_state_during_a_read(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", False)
        insert_realistic_chain(ti_db, symbol="NIFTY")
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_read(monkeypatch, trade_id=trade_id, exit_price=132.0, exit_reason="TARGET HIT")

        ti_api.get_symbol_overview("NIFTY")

        assert execution_state.get_execution(execution_id)["current_state"] == "MONITORING"

    def test_a_bug_in_reconciliation_never_breaks_the_real_read(self, ti_db, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        insert_realistic_chain(ti_db, symbol="NIFTY")
        trade_id, execution_id = self._open_trade_and_track_to_monitoring()
        self._wire_closing_read(monkeypatch, trade_id=trade_id, exit_price=132.0, exit_reason="TARGET HIT")
        monkeypatch.setattr(
            ts, "list_closed_trades",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = ti_api.get_symbol_overview("NIFTY")

        assert result["available"] is True
        assert execution_state.get_execution(execution_id)["current_state"] == "MONITORING"

    def test_no_open_trades_before_is_a_cheap_no_op(self, ti_db, monkeypatch):
        """No open trade for this symbol at all -- open_trades_before is
        empty, so _reconcile_execution_state_exits() must never even be
        called (confirmed via a poisoned stand-in that would raise if
        invoked)."""
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_EXECUTION_STATE_SHADOW", True)
        insert_realistic_chain(ti_db, symbol="NIFTY")
        monkeypatch.setattr(ai_trading_engine, "evaluate", lambda *a, **kw: _make_recommendation(action="NO_TRADE", direction=None, confidence=None))
        monkeypatch.setattr(
            ti_api, "_reconcile_execution_state_exits",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should never be called with no open trades")),
        )

        result = ti_api.get_symbol_overview("NIFTY")

        assert result["available"] is True
