"""
test_performance_analytics.py -- Milestone 23: Performance Analytics &
Strategy Intelligence. Covers both the module's own pure read/arithmetic
functions (agents/trading_intelligence/performance_analytics.py) and the
route it backs (GET /api/performance-analytics/report). Lives at repo
root, matching every other route-level test file (see
test_monitoring_center_route.py for the fixture pattern this reuses).
"""
import datetime as dt
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import (
    ai_trading_engine,
    data_access as ti_data_access,
    performance_analytics,
    ti_store,
    virtual_trailing,
)
from agents.trading_supervisor import supervision_store

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """DB path plumbing only, no Flask app -- for the module-level tests
    that call performance_analytics.* directly."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(virtual_trailing, "DB_PATH", db_path)
    monkeypatch.setattr(performance_analytics, "DB_PATH", db_path)
    app.init_db()   # creates paper_trades/scalp_paper_trades/v3_paper_trades/ti_paper_trades/virtual_trailing_state
    ti_store.init_db()
    virtual_trailing.init_db()
    return db_path


def _insert_ti_trade(db_path, *, symbol="NIFTY", direction="BUY CE", entry_price=100.0, exit_price=110.0,
                      entry_time, exit_reason="TARGET HIT", confidence=70, status="CLOSED"):
    points = round(exit_price - entry_price, 2) if (status == "CLOSED" and exit_price is not None) else None
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ti_paper_trades (symbol, direction, entry_price, exit_price, entry_time, exit_time, "
        "exit_reason, points, confidence, qty, status) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
        (symbol, direction, entry_price, exit_price if status == "CLOSED" else None, entry_time,
         entry_time if status == "CLOSED" else None, exit_reason if status == "CLOSED" else None,
         points, confidence, status),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def _insert_strategy_trade(db_path, table, *, symbol="NIFTY", points, exit_reason="TARGET HIT", status="CLOSED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"INSERT INTO {table} (symbol, points, exit_reason, status, entry_time) VALUES (?,?,?,?,?)",
        (symbol, points, exit_reason, status, dt.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


class TestHourlyHeatmap:
    def test_buckets_by_entry_hour(self, db):
        entry = dt.datetime(2026, 8, 10, 9, 15).isoformat()   # 09:xx
        for _ in range(5):
            _insert_ti_trade(db, entry_price=100.0, exit_price=110.0, entry_time=entry)
        report = performance_analytics.get_hourly_heatmap()
        assert "9" in report["by_hour"]
        bucket = report["by_hour"]["9"]
        assert bucket["total_trades"] == 5
        assert bucket["win_rate"] == 100.0
        assert bucket["sufficient_sample"] is True

    def test_below_min_sample_reports_note_not_fabricated_precision(self, db):
        entry = dt.datetime(2026, 8, 10, 14, 0).isoformat()
        _insert_ti_trade(db, entry_price=100.0, exit_price=90.0, entry_time=entry, exit_reason="STOP LOSS")
        report = performance_analytics.get_hourly_heatmap()
        bucket = report["by_hour"]["14"]
        assert bucket["total_trades"] == 1
        assert bucket["sufficient_sample"] is False
        assert bucket["note"] is not None

    def test_empty_hour_is_omitted_not_zero(self, db):
        entry = dt.datetime(2026, 8, 10, 9, 15).isoformat()
        _insert_ti_trade(db, entry_time=entry)
        report = performance_analytics.get_hourly_heatmap()
        assert "10" not in report["by_hour"]

    def test_symbol_filter(self, db):
        entry = dt.datetime(2026, 8, 10, 9, 15).isoformat()
        _insert_ti_trade(db, symbol="NIFTY", entry_time=entry)
        _insert_ti_trade(db, symbol="BANKNIFTY", entry_time=entry)
        report = performance_analytics.get_hourly_heatmap(symbol="NIFTY")
        assert report["by_hour"]["9"]["total_trades"] == 1


class TestWeekdayHeatmap:
    def test_buckets_by_weekday_name(self, db):
        entry_dt = dt.datetime(2026, 8, 10, 9, 15)   # a Monday
        assert entry_dt.strftime("%A") == "Monday"
        for _ in range(3):
            _insert_ti_trade(db, entry_time=entry_dt.isoformat())
        report = performance_analytics.get_weekday_heatmap()
        assert "Monday" in report["by_weekday"]
        assert report["by_weekday"]["Monday"]["total_trades"] == 3


class TestStrategyComparison:
    def test_all_four_engines_reported(self, db):
        out = performance_analytics.get_strategy_comparison()
        assert set(out.keys()) == {"SWING", "SCALP", "V3", "TRADING_INTELLIGENCE"}
        for name in out:
            assert out[name]["available"] is True
            assert out[name]["total_trades"] == 0

    def test_compares_across_engines_apples_to_apples(self, db):
        _insert_strategy_trade(db, "paper_trades", points=50.0)
        _insert_strategy_trade(db, "paper_trades", points=-20.0)
        _insert_strategy_trade(db, "scalp_paper_trades", points=10.0)
        _insert_ti_trade(db, entry_price=100.0, exit_price=130.0, entry_time=dt.datetime.now().isoformat())

        out = performance_analytics.get_strategy_comparison()
        assert out["SWING"]["total_trades"] == 2
        assert out["SWING"]["net_pnl"] == 30.0
        assert out["SCALP"]["total_trades"] == 1
        assert out["V3"]["total_trades"] == 0
        assert out["TRADING_INTELLIGENCE"]["total_trades"] == 1

    def test_missing_table_reports_unavailable_not_fabricated(self, db, monkeypatch):
        monkeypatch.setattr(performance_analytics, "_STRATEGY_TABLES", (("GHOST", "table_that_does_not_exist"),))
        out = performance_analytics.get_strategy_comparison()
        assert out["GHOST"]["available"] is False


class TestTrailingEfficiency:
    def test_no_states_yields_empty_distribution(self, db):
        out = performance_analytics.get_trailing_efficiency()
        assert out["stage_distribution"] == {}
        assert out["exited_sample_size"] == 0
        assert out["avg_capture_efficiency_pct"] is None

    def test_capture_efficiency_and_uplift_vs_real_trade(self, db):
        now = dt.datetime.now().isoformat()
        trade_id = _insert_ti_trade(db, entry_price=100.0, exit_price=115.0, entry_time=now, exit_reason="TARGET HIT")
        # Virtual trailing rode the same trade to +25 peak, gave back to +20 on exit --
        # captured 20/25 = 80% of the peak move, and beat the real trade's fixed +15 by +5.
        virtual_trailing.upsert_state({
            "trade_id": trade_id, "symbol": "NIFTY", "direction": "BUY CE", "strike": 100,
            "entry_price": 100.0, "original_sl_price": 90.0, "highest_premium": 125.0,
            "virtual_sl": 120.0, "virtual_target": None, "runner_mode": True, "locked_profit": 20.0,
            "state": "EXITED", "exit_price": 120.0, "exit_reason": "VIRTUAL TRAILING STOP",
            "created_ts": now, "updated_ts": now,
        })
        for _ in range(4):   # pad past MIN_SAMPLE so the average isn't suppressed
            extra_id = _insert_ti_trade(db, entry_price=100.0, exit_price=115.0, entry_time=now)
            virtual_trailing.upsert_state({
                "trade_id": extra_id, "symbol": "NIFTY", "direction": "BUY CE", "strike": 100,
                "entry_price": 100.0, "original_sl_price": 90.0, "highest_premium": 125.0,
                "virtual_sl": 120.0, "virtual_target": None, "runner_mode": True, "locked_profit": 20.0,
                "state": "EXITED", "exit_price": 120.0, "exit_reason": "VIRTUAL TRAILING STOP",
                "created_ts": now, "updated_ts": now,
            })

        out = performance_analytics.get_trailing_efficiency(symbol="NIFTY")
        assert out["exited_sample_size"] == 5
        assert out["sufficient_sample"] is True
        assert out["avg_capture_efficiency_pct"] == 80.0
        assert out["avg_trailing_uplift_points"] == 5.0

    def test_stage_distribution_covers_active_states(self, db):
        now = dt.datetime.now().isoformat()
        trade_id = _insert_ti_trade(db, entry_price=100.0, exit_price=None, entry_time=now, status="OPEN")
        virtual_trailing.upsert_state({
            "trade_id": trade_id, "symbol": "NIFTY", "direction": "BUY CE", "strike": 100,
            "entry_price": 100.0, "original_sl_price": 90.0, "highest_premium": 109.0,
            "virtual_sl": 100.0, "virtual_target": 120.0, "runner_mode": False, "locked_profit": 0.0,
            "state": "ACTIVE", "exit_price": None, "exit_reason": None,
            "created_ts": now, "updated_ts": now,
        })
        out = performance_analytics.get_trailing_efficiency()
        assert out["stage_distribution"].get("COST_PROTECTED") == 1


class TestConfidenceCalibration:
    def test_delegates_to_ai_trading_engine_unchanged(self, db):
        report = performance_analytics.get_confidence_calibration()
        assert report == ai_trading_engine.calibration_report()
        assert isinstance(report, list)
        assert len(report) == len(ai_trading_engine.CALIBRATION_BUCKETS)

    def test_dimension_breakdown_passthrough(self, db):
        report = performance_analytics.get_confidence_calibration(dimension="regime")
        assert "by_confidence" in report
        assert "by_regime" in report


class TestFullReport:
    def test_contains_every_section(self, db):
        out = performance_analytics.get_full_report()
        assert set(out.keys()) == {
            "summary", "hourly_heatmap", "weekday_heatmap", "strategy_comparison",
            "trailing_efficiency", "confidence_calibration",
        }
        assert out["summary"]["total_trades"] == 0


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(virtual_trailing, "DB_PATH", db_path)
    monkeypatch.setattr(performance_analytics, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


CSRF_TOKEN = "test-csrf-token"
ROUTE = "/api/performance-analytics/report"


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["csrf_token"] = CSRF_TOKEN


class TestFeatureFlagDefaultDisabled:
    def test_404_when_flag_is_off(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", False)
        _login_admin(client)
        resp = client.get(ROUTE)
        assert resp.status_code == 404


class TestAuthWhenEnabled:
    def test_unauthenticated_redirects_to_login(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        resp = client.get(ROUTE)
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_pa@example.com", "sub_pa", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get(ROUTE)
        assert resp.status_code == 403


class TestBehaviorWhenEnabled:
    def test_empty_state_returns_every_section(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        _login_admin(client)
        resp = client.get(ROUTE)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["summary"]["total_trades"] == 0
        assert data["strategy_comparison"]["TRADING_INTELLIGENCE"]["available"] is True
        assert data["trailing_efficiency"]["stage_distribution"] == {}
        assert isinstance(data["confidence_calibration"], list)

    def test_symbol_filter_scopes_summary(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        _login_admin(client)
        _insert_ti_trade(app.DB_PATH, symbol="NIFTY", entry_price=100.0, exit_price=110.0,
                          entry_time=dt.datetime.now().isoformat())
        resp = client.get(ROUTE + "?symbol=BANKNIFTY")
        assert resp.get_json()["summary"]["total_trades"] == 0
        resp = client.get(ROUTE + "?symbol=NIFTY")
        assert resp.get_json()["summary"]["total_trades"] == 1

    def test_invalid_dimension_is_400(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        _login_admin(client)
        resp = client.get(ROUTE + "?dimension=not_a_real_dimension")
        assert resp.status_code == 400

    def test_valid_dimension_changes_calibration_shape(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_PERFORMANCE_ANALYTICS_UI", True)
        _login_admin(client)
        resp = client.get(ROUTE + "?dimension=quality_tier")
        assert resp.status_code == 200
        assert "by_quality_tier" in resp.get_json()["confidence_calibration"]
