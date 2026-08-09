"""
test_intelligence_orchestrator.py -- Milestone 13, Phase 1: regression
tests for intelligence_orchestrator.py / intelligence_models.py and the
new GET /api/intelligence/snapshot route.

NOTE on file location: the Phase 1 brief requested "backend/tests/
runtime/test_intelligence_orchestrator.py". No "backend/" directory (and
no "tests/" directory of any kind) exists anywhere in this repository --
every one of its 20+ existing test files (test_auth.py, test_shadow_
mode_read_only.py, test_shadow_mode_cli.py, test_market_hours.py, ...)
lives at the repo root as test_*.py, matching pytest's default rootdir
discovery. This file follows that established, exclusive convention
instead of introducing a new, inconsistent directory layout -- the same
call already made and accepted for mcx_session_config.py's and
test_shadow_mode_cli.py's own locations.

Same SKIP_AUTOSTART=1 + throwaway-DB technique as every other route-
level test file in this project: no live thread, no real oi_history.db
touched by any test here.
"""
import datetime as dt
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
import intelligence_orchestrator
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_supervisor import supervision_store
from agents import audit_log, event_bus

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Full Flask test client on a throwaway DB -- app.init_db() already
    creates the real cycles/strikes/market_structure_snapshots schema
    (app.py:2530-2549, 2797-...), so no separate hand-rolled schema is
    needed the way test_shadow_mode_cli.py's cli_db fixture needs one
    (that fixture avoids importing app.py at all; this one deliberately
    uses the real Flask app to exercise the actual route)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id


def _seed_user(db_path, *, email, role):
    now = dt.datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
        "VALUES (?,?,?,?,1,?,?)",
        (email, email.split("@")[0], "x", role, now, now),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def _insert_realistic_chain(db_path, *, symbol="NIFTY", underlying_ltp=25000.0, atm=25000.0,
                             pcr=0.9, step=50, strikes_each_side=4,
                             ce_vol=15000, pe_vol=8000, ce_signal="Neutral", pe_signal="Neutral"):
    """Same technique test_shadow_mode_cli.py's own _insert_realistic_chain()
    already established -- writes directly into cycles/strikes so
    agents.trading_intelligence.market_data.get_snapshot() has real data
    to build a signal from, without ever going through app.py's live
    broker-fetching loop."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, max_pain, bias) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, dt.datetime.now().isoformat(), "2026-08-08", "10:00:00", underlying_ltp, atm, pcr, atm, None),
    )
    cycle_id = cur.lastrowid
    for i in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + i * step
        conn.execute(
            "INSERT INTO strikes (cycle_id, strike, ce_oi, ce_oi_chg, ce_vol, ce_ltp, ce_chg_pct, ce_signal, "
            "pe_oi, pe_oi_chg, pe_vol, pe_ltp, pe_chg_pct, pe_signal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, strike, 50000, 500, ce_vol, 100.0, 1.0, ce_signal, 60000, 500, pe_vol, 80.0, 0.5, pe_signal),
        )
    conn.commit()
    conn.close()


# --- 5. pure read-only behaviour: no data -> honest None, never a crash -----

class TestNoDataDegradesHonestly:
    def test_build_snapshot_returns_none_without_any_cycle(self, client):
        assert intelligence_orchestrator.build_snapshot("NIFTY") is None

    def test_build_snapshot_returns_none_for_an_unknown_symbol(self, client):
        _insert_realistic_chain(app.DB_PATH, symbol="NIFTY")
        assert intelligence_orchestrator.build_snapshot("BANKNIFTY") is None


# --- 1 & 2. engine adapters: normalized 0-100 scores, real data in ----------

class TestEngineAdaptersProduceNormalizedScores:
    def test_snapshot_shape_and_score_ranges(self, client):
        _insert_realistic_chain(app.DB_PATH)
        snap = intelligence_orchestrator.build_snapshot("NIFTY")
        assert snap is not None
        assert snap.symbol == "NIFTY"
        for field in ("confidence", "oi_strength", "probability_score", "volume_score", "institutional_score"):
            value = getattr(snap, field)
            assert isinstance(value, int), f"{field} should be an int, got {type(value)}"
            assert 0 <= value <= 100, f"{field}={value} out of 0-100 range"

    def test_volume_score_scales_with_real_ce_pe_volume(self, client):
        """Same symbol, same book shape -- only the raw ce_vol/pe_vol
        differ -- so volume_score must move with it (derived directly
        from _volume_and_liquidity(), see module docstring: no
        dedicated 'volume engine' module exists, this IS that adapter)."""
        _insert_realistic_chain(app.DB_PATH, symbol="THIN", ce_vol=100, pe_vol=100)
        _insert_realistic_chain(app.DB_PATH, symbol="LIQUID", ce_vol=40000, pe_vol=40000)
        thin = intelligence_orchestrator.build_snapshot("THIN")
        liquid = intelligence_orchestrator.build_snapshot("LIQUID")
        assert thin.volume_score < liquid.volume_score

    def test_volume_score_caps_at_100(self, client):
        _insert_realistic_chain(app.DB_PATH, symbol="MEGA", ce_vol=10_000_000, pe_vol=10_000_000)
        snap = intelligence_orchestrator.build_snapshot("MEGA")
        assert snap.volume_score == 100


# --- 3. bias resolution -- pure adapter-level tests over the real 7-zone ----
# vocabulary compute_trend_meter() returns, so this doesn't depend on
# crafting option-chain data that reliably drives a specific bias end to
# end (fragile) -- it verifies the actual normalization function.

class TestBiasResolution:
    @pytest.mark.parametrize("zone", ["STRONG BULLISH", "BULLISH", "WEAK BULLISH"])
    def test_bullish_zones_normalize_to_bullish(self, zone):
        assert intelligence_orchestrator._normalize_bias(zone) == "BULLISH"

    @pytest.mark.parametrize("zone", ["STRONG BEARISH", "BEARISH", "WEAK BEARISH"])
    def test_bearish_zones_normalize_to_bearish(self, zone):
        assert intelligence_orchestrator._normalize_bias(zone) == "BEARISH"

    def test_neutral_zone_normalizes_to_neutral(self):
        assert intelligence_orchestrator._normalize_bias("NEUTRAL") == "NEUTRAL"

    def test_build_snapshot_bias_is_always_one_of_the_three(self, client):
        _insert_realistic_chain(app.DB_PATH)
        snap = intelligence_orchestrator.build_snapshot("NIFTY")
        assert snap.bias in ("BULLISH", "BEARISH", "NEUTRAL")


class TestGreeksAlignmentAdapter:
    def test_ce_direction_is_bullish_lean(self):
        signal = {"delta_used": 0.55, "direction": "CE"}
        assert intelligence_orchestrator._greeks_alignment(signal) == "BULLISH LEAN"

    def test_pe_direction_is_bearish_lean(self):
        signal = {"delta_used": -0.5, "direction": "PE"}
        assert intelligence_orchestrator._greeks_alignment(signal) == "BEARISH LEAN"

    def test_missing_delta_is_unavailable(self):
        signal = {"delta_used": None, "direction": "CE"}
        assert intelligence_orchestrator._greeks_alignment(signal) == "UNAVAILABLE"

    def test_no_direction_is_neutral(self):
        signal = {"delta_used": 0.1, "direction": None}
        assert intelligence_orchestrator._greeks_alignment(signal) == "NEUTRAL"

    def test_build_snapshot_greeks_alignment_is_a_valid_value(self, client):
        _insert_realistic_chain(app.DB_PATH)
        snap = intelligence_orchestrator.build_snapshot("NIFTY")
        assert snap.greeks_alignment in ("BULLISH LEAN", "BEARISH LEAN", "NEUTRAL", "UNAVAILABLE")


# --- 4. deterministic aggregation, no external API calls --------------------

class TestDeterministicAggregation:
    def test_build_snapshot_is_deterministic_for_identical_data(self, client):
        _insert_realistic_chain(app.DB_PATH)
        first = intelligence_orchestrator.build_snapshot("NIFTY")
        second = intelligence_orchestrator.build_snapshot("NIFTY")
        assert first == second

    def test_probability_score_is_the_mean_of_the_three_sub_scores(self, client):
        _insert_realistic_chain(app.DB_PATH)
        snap = intelligence_orchestrator.build_snapshot("NIFTY")
        import statistics
        expected = round(statistics.mean([snap.oi_strength, snap.institutional_score, snap.confidence]))
        assert snap.probability_score == expected


# --- Read-only / no side effects --------------------------------------------

class TestPureReadOnly:
    def test_build_snapshot_never_writes_a_row(self, client):
        _insert_realistic_chain(app.DB_PATH)
        conn = sqlite3.connect(app.DB_PATH)
        before = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("cycles", "strikes")}
        conn.close()

        intelligence_orchestrator.build_snapshot("NIFTY")
        intelligence_orchestrator.build_snapshot("NIFTY")

        conn = sqlite3.connect(app.DB_PATH)
        after = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                 for name in ("cycles", "strikes")}
        conn.close()
        assert before == after


# --- API route: GET-only, admin-gated, honest 400/404 ------------------------

class TestSnapshotRoute:
    def test_missing_symbol_returns_400(self, client):
        _login_admin(client)
        resp = client.get("/api/intelligence/snapshot")
        assert resp.status_code == 400

    def test_unknown_symbol_returns_404_not_a_fabricated_snapshot(self, client):
        _login_admin(client)
        resp = client.get("/api/intelligence/snapshot?symbol=NOTREAL")
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    def test_valid_symbol_returns_200_with_full_snapshot_shape(self, client):
        _insert_realistic_chain(app.DB_PATH, symbol="NIFTY")
        _login_admin(client)
        resp = client.get("/api/intelligence/snapshot?symbol=NIFTY")
        assert resp.status_code == 200
        data = resp.get_json()
        for field in ("symbol", "bias", "confidence", "oi_strength", "probability_score",
                      "volume_score", "greeks_alignment", "institutional_score"):
            assert field in data
        assert data["symbol"] == "NIFTY"

    def test_post_returns_405(self, client):
        _login_admin(client)
        resp = client.post("/api/intelligence/snapshot?symbol=NIFTY")
        assert resp.status_code == 405

    def test_unauthenticated_is_redirected_to_login(self, client):
        resp = client.get("/api/intelligence/snapshot?symbol=NIFTY")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        user_id = _seed_user(app.DB_PATH, email="sub@example.com", role="subscriber")
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
        resp = client.get("/api/intelligence/snapshot?symbol=NIFTY")
        assert resp.status_code == 403
