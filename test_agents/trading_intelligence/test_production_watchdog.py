"""
test_agents/trading_intelligence/test_production_watchdog.py -- Milestone
22: regression tests for production_watchdog.py, the Production
Watchdog. Mocks every external dependency each individual check reads
(lifecycle.get_runtime_status(), ai_live_snapshot.build_ai_live_snapshot(),
retry_tracker.get_due_retries(), sysadmin_report/sysadmin_store,
telegram_notifier, runtime_events) -- this file tests the CHECK/
ESCALATION/METRICS logic, not those other modules' own internals.
"""
import datetime as dt
import json

import pytest

from agents import config
from agents.intelligence_alerts import retry_tracker
from agents.runtime import runtime_events
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import ai_live_snapshot, production_watchdog as pw
from agents.trading_intelligence import telegram_notifier, ti_store, virtual_trailing as vt


@pytest.fixture(autouse=True)
def _retry_tracker_db(ti_db, monkeypatch):
    """production_watchdog's telegram_backlog check reaches into
    agents.intelligence_alerts.retry_tracker, which owns its own table
    outside this package -- isolate it the same way ti_db isolates
    everything else, rather than letting it touch the real oi_history.db
    relative to whatever the test process's cwd happens to be."""
    monkeypatch.setattr(retry_tracker, "DB_PATH", ti_db)
    retry_tracker.init_db()


def _state(*, trade_id, symbol="NIFTY", entry_price=100.0):
    return vt._init_state(
        {"id": trade_id, "symbol": symbol, "direction": "CE", "entry_price": entry_price,
         "sl_price": entry_price - 10, "target_price": entry_price + 20, "strike": 24500},
        now="2026-08-14T09:15:00",
    )


class TestSchedulerHeartbeatCheck:
    def test_fails_when_watchdog_stale(self, monkeypatch):
        from agents.runtime import lifecycle
        monkeypatch.setattr(lifecycle, "get_runtime_status",
                             lambda: {"watchdog_stale": True, "last_cycle_timestamp": dt.datetime.now().isoformat()})
        result = pw._check_scheduler_heartbeat()
        assert result.ok is False

    def test_fails_when_never_run(self, monkeypatch):
        from agents.runtime import lifecycle
        monkeypatch.setattr(lifecycle, "get_runtime_status", lambda: {"watchdog_stale": False, "last_cycle_timestamp": None})
        result = pw._check_scheduler_heartbeat()
        assert result.ok is False
        assert "never" in result.detail

    def test_fails_when_stale_beyond_threshold(self, monkeypatch):
        from agents.runtime import lifecycle
        old = (dt.datetime.now() - dt.timedelta(seconds=pw.SCHEDULER_STALE_SECONDS + 60)).isoformat()
        monkeypatch.setattr(lifecycle, "get_runtime_status", lambda: {"watchdog_stale": False, "last_cycle_timestamp": old})
        result = pw._check_scheduler_heartbeat()
        assert result.ok is False

    def test_ok_when_fresh(self, monkeypatch):
        from agents.runtime import lifecycle
        recent = dt.datetime.now().isoformat()
        monkeypatch.setattr(lifecycle, "get_runtime_status", lambda: {"watchdog_stale": False, "last_cycle_timestamp": recent})
        result = pw._check_scheduler_heartbeat()
        assert result.ok is True


class TestVirtualTrailingCycleCheck:
    def test_ok_and_skipped_when_feature_disabled(self, ti_db, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_VIRTUAL_TRAILING", False)
        result = pw._check_virtual_trailing_cycle()
        assert result.ok is True
        assert "skipped" in result.detail

    def test_fails_when_never_run(self, ti_db, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_VIRTUAL_TRAILING", True)
        result = pw._check_virtual_trailing_cycle()
        assert result.ok is False

    def test_ok_when_recent(self, ti_db, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_VIRTUAL_TRAILING", True)
        vt.record_cycle_duration(42.0)
        result = pw._check_virtual_trailing_cycle()
        assert result.ok is True

    def test_fails_when_stale(self, ti_db, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_VIRTUAL_TRAILING", True)
        old = (dt.datetime.now() - dt.timedelta(seconds=pw.VIRTUAL_TRAILING_STALE_SECONDS + 60)).isoformat()
        vt.record_cycle_duration(42.0, now=old)
        result = pw._check_virtual_trailing_cycle()
        assert result.ok is False


class TestAiLiveSnapshotCheck:
    def test_ok_when_snapshot_builds(self, monkeypatch):
        monkeypatch.setattr(ai_live_snapshot, "build_ai_live_snapshot", lambda symbol: {"symbol": symbol})
        result = pw._check_ai_live_snapshot()
        assert result.ok is True
        assert result.latency_ms is not None

    def test_fails_when_none(self, monkeypatch):
        monkeypatch.setattr(ai_live_snapshot, "build_ai_live_snapshot", lambda symbol: None)
        result = pw._check_ai_live_snapshot()
        assert result.ok is False

    def test_fails_when_it_raises(self, monkeypatch):
        def _raise(symbol):
            raise RuntimeError("boom")
        monkeypatch.setattr(ai_live_snapshot, "build_ai_live_snapshot", _raise)
        result = pw._check_ai_live_snapshot()
        assert result.ok is False
        assert "boom" in result.detail


class TestDbHealthCheck:
    def test_ok_round_trip(self, ti_db):
        result = pw._check_db_health()
        assert result.ok is True
        assert result.latency_ms is not None


class TestTelegramBacklogCheck:
    def test_ok_when_under_threshold(self, ti_db):
        result = pw._check_telegram_backlog()
        assert result.ok is True
        assert "0 due retries" in result.detail

    def test_fails_when_over_threshold(self, ti_db, monkeypatch):
        monkeypatch.setattr(retry_tracker, "get_due_retries", lambda now=None: list(range(pw.TELEGRAM_BACKLOG_MAX + 1)))
        result = pw._check_telegram_backlog()
        assert result.ok is False


class TestControlCenterTradesCheck:
    def test_ok_reports_count(self, ti_db):
        vt.upsert_state(_state(trade_id=1))
        vt.upsert_state(_state(trade_id=2, symbol="BANKNIFTY"))
        result = pw._check_control_center_trades()
        assert result.ok is True
        assert "2 tracked trades" in result.detail


class TestRunCheckNeverRaises:
    def test_a_check_that_raises_is_caught_as_a_failure(self, monkeypatch):
        def _raise():
            raise RuntimeError("kaboom")
        monkeypatch.setitem(pw._CHECK_FUNCS, "db_health", _raise)
        result = pw._run_check("db_health")
        assert result.ok is False
        assert "kaboom" in result.detail


class TestEscalation:
    def test_escalates_exactly_at_the_threshold(self, ti_db, monkeypatch):
        calls = []
        monkeypatch.setattr(sysadmin_store, "record_report", lambda report: calls.append("report"))
        monkeypatch.setattr(telegram_notifier, "send_admin_alert", lambda text: calls.append("telegram"))
        monkeypatch.setattr(runtime_events, "emit_safe", lambda *a, **kw: calls.append("event"))

        conn = pw._connect()
        try:
            result = pw.CheckResult("db_health", False, "simulated failure")
            for i in range(pw.CONSECUTIVE_FAILURES_BEFORE_ESCALATION - 1):
                pw._update_check_state(conn, result, now=dt.datetime.now().isoformat())
                conn.commit()
                assert calls == []   # not yet at threshold
            pw._update_check_state(conn, result, now=dt.datetime.now().isoformat())
            conn.commit()
            assert calls == ["report", "telegram", "event"]
        finally:
            conn.close()

    def test_does_not_re_escalate_every_subsequent_cycle(self, ti_db, monkeypatch):
        calls = []
        monkeypatch.setattr(sysadmin_store, "record_report", lambda report: calls.append(1))
        monkeypatch.setattr(telegram_notifier, "send_admin_alert", lambda text: calls.append(1))
        monkeypatch.setattr(runtime_events, "emit_safe", lambda *a, **kw: None)

        conn = pw._connect()
        try:
            result = pw.CheckResult("db_health", False, "still failing")
            for _ in range(pw.CONSECUTIVE_FAILURES_BEFORE_ESCALATION + 3):
                pw._update_check_state(conn, result, now=dt.datetime.now().isoformat())
                conn.commit()
            # Telegram + report each fire exactly once for this whole streak, not once per cycle.
            assert calls.count(1) == 2
        finally:
            conn.close()

    def test_recovery_resets_the_streak(self, ti_db, monkeypatch):
        monkeypatch.setattr(sysadmin_store, "record_report", lambda report: None)
        monkeypatch.setattr(telegram_notifier, "send_admin_alert", lambda text: None)
        monkeypatch.setattr(runtime_events, "emit_safe", lambda *a, **kw: None)

        conn = pw._connect()
        try:
            failing = pw.CheckResult("db_health", False, "failing")
            for _ in range(pw.CONSECUTIVE_FAILURES_BEFORE_ESCALATION):
                pw._update_check_state(conn, failing, now=dt.datetime.now().isoformat())
                conn.commit()
            recovered = pw.CheckResult("db_health", True, "back to normal")
            consecutive_failures = pw._update_check_state(conn, recovered, now=dt.datetime.now().isoformat())
            conn.commit()
            assert consecutive_failures == 0
            state = pw._get_check_state(conn, "db_health")
            assert bool(state["escalated"]) is False
        finally:
            conn.close()


class TestRunWatchdogCycle:
    def test_returns_all_six_checks_and_logs_one_cycle_row(self, ti_db):
        result = pw.run_watchdog_cycle()
        assert set(result["checks"].keys()) == set(pw.CHECK_NAMES)
        assert result["duration_ms"] >= 0

        conn = pw._connect()
        try:
            rows = conn.execute("SELECT * FROM watchdog_cycle_log").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1

    def test_rolling_window_trims_old_rows(self, ti_db):
        conn = pw._connect()
        try:
            for i in range(pw.CYCLE_LOG_RETENTION_ROWS + 10):
                conn.execute(
                    "INSERT INTO watchdog_cycle_log (ts, duration_ms, overall_ok, checks_json) VALUES (?,?,?,?)",
                    (dt.datetime.now().isoformat(), 1.0, 1, "{}"),
                )
            conn.commit()
        finally:
            conn.close()
        pw.run_watchdog_cycle()
        conn = pw._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM watchdog_cycle_log").fetchone()[0]
        finally:
            conn.close()
        assert count == pw.CYCLE_LOG_RETENTION_ROWS


class TestGetMetrics:
    def test_empty_log_reports_honest_nones(self, ti_db):
        m = pw.get_metrics()
        assert m["avg_cycle_ms"] is None
        assert m["max_cycle_ms"] is None
        assert m["last_successful_cycle"] is None
        assert m["trailing_exits_last_1h"] == 0

    def test_computes_avg_max_and_last_successful(self, ti_db):
        conn = pw._connect()
        try:
            now = dt.datetime.now()
            rows = [
                ((now - dt.timedelta(minutes=3)).isoformat(), 100.0, 1, json.dumps({"ai_live_snapshot": {"latency_ms": 12.5}})),
                ((now - dt.timedelta(minutes=2)).isoformat(), 300.0, 0, json.dumps({})),
                ((now - dt.timedelta(minutes=1)).isoformat(), 200.0, 1, json.dumps({"ai_live_snapshot": {"latency_ms": 8.0}})),
            ]
            for ts, duration, ok, checks_json in rows:
                conn.execute(
                    "INSERT INTO watchdog_cycle_log (ts, duration_ms, overall_ok, checks_json) VALUES (?,?,?,?)",
                    (ts, duration, ok, checks_json),
                )
            conn.commit()
        finally:
            conn.close()

        m = pw.get_metrics()
        assert m["avg_cycle_ms"] == 200.0
        assert m["max_cycle_ms"] == 300.0
        assert m["last_successful_cycle"] is not None   # the most recent OK row (1 min ago)
        assert m["snapshot_refresh_latency_ms"] == 8.0   # most recent row's own latency

    def test_trailing_exits_last_1h_counts_recently_exited_rows_only(self, ti_db):
        now = dt.datetime.now()
        recent = vt.evaluate_trade(_state(trade_id=1), 90.0)   # exits immediately (below original SL)
        recent["updated_ts"] = now.isoformat()
        vt.upsert_state(recent)
        old = vt.evaluate_trade(_state(trade_id=2), 90.0)
        old["updated_ts"] = (now - dt.timedelta(hours=2)).isoformat()
        vt.upsert_state(old)

        m = pw.get_metrics()
        assert m["trailing_exits_last_1h"] == 1


class TestGetStatus:
    def test_widget_defaults_healthy_with_no_data_yet(self, ti_db):
        status = pw.get_status()
        assert status["widget"] == {
            "Scheduler": "RUNNING", "Snapshot": "LIVE", "Trailing Engine": "ACTIVE",
            "DB": "HEALTHY", "Telegram": "CONNECTED",
        }

    def test_widget_reflects_a_failing_check(self, ti_db):
        conn = pw._connect()
        try:
            pw._update_check_state(conn, pw.CheckResult("db_health", False, "down"), now=dt.datetime.now().isoformat())
            conn.commit()
        finally:
            conn.close()
        status = pw.get_status()
        assert status["widget"]["DB"] == "DEGRADED"
        assert status["widget"]["Scheduler"] == "RUNNING"   # untouched checks stay healthy
