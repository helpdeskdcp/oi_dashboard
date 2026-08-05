"""
test_backtest_profiles.py -- regression tests for the backtest_profiles table
and its CRUD/activate routes (app.py, added for the per-symbol parameter
tuning feature). Same SKIP_AUTOSTART / throwaway-DB technique as test_auth.py
-- no live data threads, no real oi_history.db touched.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import json
import sqlite3

import pytest

import app
import auth


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.state.get("sr_active_profile_cache", {}).clear()   # global in-memory cache -- each test gets a fresh DB, must not leak stale reads across them
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _admin_user_id(db_path):
    conn = sqlite3.connect(db_path)
    uid = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    return uid


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _csrf(client, token="test-csrf-token-value"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def _login_admin(client):
    uid = _admin_user_id(app.DB_PATH)
    _login_session(client, uid)
    return uid


class TestParamValidation:
    def test_unknown_keys_dropped_known_keys_clamped(self):
        clean, errors = app._validate_profile_params("sr", {
            "min_risk_reward": 999,        # above max=20 -> clamped
            "premium_ema_fast": "7",       # numeric-string -> coerced to int
            "not_a_real_param": "x",       # unknown -> silently dropped
        })
        assert errors == []
        assert clean["min_risk_reward"] == 20
        assert clean["premium_ema_fast"] == 7
        assert "not_a_real_param" not in clean

    def test_bad_type_reports_error(self):
        clean, errors = app._validate_profile_params("sr", {"min_risk_reward": "not-a-number"})
        assert errors
        assert "min_risk_reward" not in clean

    def test_v2_has_no_tunables(self):
        assert app.ENGINE_PARAM_SPECS.get("v2") in (None,)


class TestMaxSlAtrMultSpecAndValidation:
    """max_sl_atr_mult (Maximum Stop Loss ATR Multiplier, dynamic-sr-v4) is
    the one nullable tunable -- unlike every other knob, its "disabled"
    state must be distinguishable from "never configured" so a saved
    profile restores the UI checkbox correctly, not just the number (see
    _validate_profile_params' docstring)."""

    def test_spec_matches_requirements(self):
        spec = app.ENGINE_PARAM_SPECS["dynamic-sr-v4"]["max_sl_atr_mult"]
        assert spec["default"] == 1.5
        assert spec["min"] == 0.5
        assert spec["max"] == 3.0
        assert spec["step"] == 0.1
        assert spec["nullable"] is True

    def test_disabled_via_none_is_stored_as_explicit_none_not_dropped(self):
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": None})
        assert errors == []
        assert "max_sl_atr_mult" in clean
        assert clean["max_sl_atr_mult"] is None

    def test_disabled_via_blank_string_is_stored_as_explicit_none(self):
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": ""})
        assert errors == []
        assert clean["max_sl_atr_mult"] is None

    def test_absent_key_stays_absent_distinct_from_explicit_none(self):
        # No key at all (e.g. a profile saved before this knob existed, or
        # any other engine's params) -- must NOT appear in clean, so
        # overrides.get() in _run_backtest_job falls through to None exactly
        # like today, and the UI can tell "never configured" (show the
        # enabled default) apart from "explicitly disabled" (show unchecked).
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"atr_trail_mult": 2.0})
        assert errors == []
        assert "max_sl_atr_mult" not in clean

    def test_enabled_value_is_clamped_like_any_other_param(self):
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": 5.0})
        assert errors == []
        assert clean["max_sl_atr_mult"] == 3.0   # clamped to max

        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": 0.1})
        assert errors == []
        assert clean["max_sl_atr_mult"] == 0.5   # clamped to min

    def test_enabled_value_within_range_passes_through(self):
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": 2.0})
        assert errors == []
        assert clean["max_sl_atr_mult"] == 2.0

    def test_bad_type_still_reports_an_error(self):
        clean, errors = app._validate_profile_params("dynamic-sr-v4", {"max_sl_atr_mult": "not-a-number"})
        assert errors
        assert "max_sl_atr_mult" not in clean


class TestProfileCRUD:
    def test_save_then_list(self, client):
        token = _csrf(client)
        _login_admin(client)
        resp = client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "NIFTY", "engine": "sr", "profile_name": "Profile 1",
            "params_json": json.dumps({"min_risk_reward": 2.0}),
        })
        assert resp.status_code == 302

        listed = client.get("/api/backtest_profiles?symbol=NIFTY&engine=sr")
        assert listed.status_code == 200
        rows = listed.get_json()
        assert len(rows) == 1
        assert rows[0]["profile_name"] == "Profile 1"
        assert rows[0]["params"]["min_risk_reward"] == 2.0
        assert rows[0]["is_active_live"] is False

    def test_save_upserts_on_same_name(self, client):
        token = _csrf(client)
        _login_admin(client)
        for rr in (2.0, 3.5):
            client.post("/backtest/profile/save", data={
                "csrf_token": token, "symbol": "NIFTY", "engine": "sr", "profile_name": "Profile 1",
                "params_json": json.dumps({"min_risk_reward": rr}),
            })
        rows = client.get("/api/backtest_profiles?symbol=NIFTY&engine=sr").get_json()
        assert len(rows) == 1   # upsert, not a duplicate row
        assert rows[0]["params"]["min_risk_reward"] == 3.5

    def test_load_prefills_job_form(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "BANKNIFTY", "engine": "sr", "profile_name": "Profile 2",
            "params_json": json.dumps({"min_risk_reward": 2.5}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=BANKNIFTY&engine=sr").get_json()[0]["id"]

        resp = client.post("/backtest/profile/load", data={"csrf_token": token, "profile_id": profile_id})
        assert resp.status_code == 302
        loaded_form = app.state["backtest_job"]["form"]
        assert loaded_form["symbol"] == "BANKNIFTY"
        assert loaded_form["engine"] == "sr"
        assert json.loads(loaded_form["profile_params"])["min_risk_reward"] == 2.5

    def test_max_sl_atr_mult_disabled_state_round_trips_through_save_and_load(self, client):
        # A profile that explicitly disabled the SL cap must reload with the
        # UI checkbox unchecked (explicit null), not "never configured"
        # (absent key, which the UI would instead show enabled at 1.5).
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "NIFTY", "engine": "dynamic-sr-v4", "profile_name": "No SL cap",
            "params_json": json.dumps({"atr_trail_mult": 2.0, "max_sl_atr_mult": None}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=NIFTY&engine=dynamic-sr-v4").get_json()[0]["id"]

        resp = client.post("/backtest/profile/load", data={"csrf_token": token, "profile_id": profile_id})
        assert resp.status_code == 302
        loaded_params = json.loads(app.state["backtest_job"]["form"]["profile_params"])
        assert "max_sl_atr_mult" in loaded_params   # explicit key, not dropped
        assert loaded_params["max_sl_atr_mult"] is None
        assert loaded_params["atr_trail_mult"] == 2.0

    def test_max_sl_atr_mult_enabled_value_round_trips_through_save_and_load(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "NIFTY", "engine": "dynamic-sr-v4", "profile_name": "Tight SL cap",
            "params_json": json.dumps({"max_sl_atr_mult": 1.25}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=NIFTY&engine=dynamic-sr-v4").get_json()[0]["id"]

        resp = client.post("/backtest/profile/load", data={"csrf_token": token, "profile_id": profile_id})
        assert resp.status_code == 302
        loaded_params = json.loads(app.state["backtest_job"]["form"]["profile_params"])
        assert loaded_params["max_sl_atr_mult"] == 1.25

    def test_delete_removes_row(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "NIFTY", "engine": "v3", "profile_name": "ToDelete",
            "params_json": json.dumps({"min_risk_reward": 1.8}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=NIFTY&engine=v3").get_json()[0]["id"]
        del_resp = client.post("/backtest/profile/delete", data={"csrf_token": token, "profile_id": profile_id})
        assert del_resp.status_code == 302
        rows = client.get("/api/backtest_profiles?symbol=NIFTY&engine=v3").get_json()
        assert rows == []


class TestLiveActivation:
    def test_only_sr_engine_activatable(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "NIFTY", "engine": "dynamic-sr", "profile_name": "V1 Tuned",
            "params_json": json.dumps({"min_tradeable_confidence": 65}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=NIFTY&engine=dynamic-sr").get_json()[0]["id"]
        resp = client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": profile_id})
        assert resp.status_code == 400
        assert "sr" in resp.get_json()["error"]

    def test_activating_second_profile_deactivates_first(self, client):
        token = _csrf(client)
        _login_admin(client)
        for name, rr in (("Profile A", 1.5), ("Profile B", 2.5)):
            client.post("/backtest/profile/save", data={
                "csrf_token": token, "symbol": "NATURALGAS", "engine": "sr", "profile_name": name,
                "params_json": json.dumps({"min_risk_reward": rr}),
            })
        rows = client.get("/api/backtest_profiles?symbol=NATURALGAS&engine=sr").get_json()
        id_a = next(r["id"] for r in rows if r["profile_name"] == "Profile A")
        id_b = next(r["id"] for r in rows if r["profile_name"] == "Profile B")

        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": id_a})
        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": id_b})

        rows = client.get("/api/backtest_profiles?symbol=NATURALGAS&engine=sr").get_json()
        active = [r for r in rows if r["is_active_live"]]
        assert len(active) == 1
        assert active[0]["profile_name"] == "Profile B"

    def test_deactivate_clears_flag(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "GOLD", "engine": "sr", "profile_name": "Profile 3",
            "params_json": json.dumps({"min_risk_reward": 2.0}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=GOLD&engine=sr").get_json()[0]["id"]
        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": profile_id})
        assert client.get("/api/backtest_profiles?symbol=GOLD&engine=sr").get_json()[0]["is_active_live"] is True

        client.post("/backtest/profile/deactivate", data={"csrf_token": token, "profile_id": profile_id})
        assert client.get("/api/backtest_profiles?symbol=GOLD&engine=sr").get_json()[0]["is_active_live"] is False


class TestLiveParamsResolution:
    """get_sr_live_params is the Phase 4 wiring: the live loop's actual read
    path for every 'sr'-engine tunable. No Angel One / live loop involved --
    this calls the resolver function directly, same as backtest_profiles'
    own CRUD tests above."""

    def test_no_active_profile_falls_back_to_defaults(self, client):
        params = app.get_sr_live_params("NIFTY")
        assert params["min_risk_reward"] == app.ENGINE_PARAM_SPECS["sr"]["min_risk_reward"]["default"]
        assert params["max_hold_minutes"] == app.ENGINE_PARAM_SPECS["sr"]["max_hold_minutes"]["default"]
        assert "sr_cooldown_minutes" not in params   # backtest_only, never in the live dict

    def test_activation_overrides_and_cache_reflects_it_immediately(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "BANKNIFTY", "engine": "sr", "profile_name": "Tuned",
            "params_json": json.dumps({"min_risk_reward": 3.0, "max_hold_minutes": 45}),
        })
        # Populate the cache with defaults BEFORE activation, to prove
        # activation invalidates it rather than serving a stale read.
        assert app.get_sr_live_params("BANKNIFTY")["min_risk_reward"] != 3.0

        profile_id = client.get("/api/backtest_profiles?symbol=BANKNIFTY&engine=sr").get_json()[0]["id"]
        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": profile_id})

        params = app.get_sr_live_params("BANKNIFTY")
        assert params["min_risk_reward"] == 3.0
        assert params["max_hold_minutes"] == 45
        # Untouched keys still fall back to defaults, not left missing.
        assert params["proximity_atr_mult"] == app.ENGINE_PARAM_SPECS["sr"]["proximity_atr_mult"]["default"]

    def test_deactivate_reverts_cache_to_defaults(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "SENSEX", "engine": "sr", "profile_name": "Tuned",
            "params_json": json.dumps({"min_risk_reward": 4.0}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=SENSEX&engine=sr").get_json()[0]["id"]
        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": profile_id})
        assert app.get_sr_live_params("SENSEX")["min_risk_reward"] == 4.0

        client.post("/backtest/profile/deactivate", data={"csrf_token": token, "profile_id": profile_id})
        assert app.get_sr_live_params("SENSEX")["min_risk_reward"] == app.ENGINE_PARAM_SPECS["sr"]["min_risk_reward"]["default"]

    def test_symbols_are_isolated(self, client):
        token = _csrf(client)
        _login_admin(client)
        client.post("/backtest/profile/save", data={
            "csrf_token": token, "symbol": "CRUDEOIL", "engine": "sr", "profile_name": "Tuned",
            "params_json": json.dumps({"min_risk_reward": 5.0}),
        })
        profile_id = client.get("/api/backtest_profiles?symbol=CRUDEOIL&engine=sr").get_json()[0]["id"]
        client.post("/backtest/profile/activate", data={"csrf_token": token, "profile_id": profile_id})

        assert app.get_sr_live_params("CRUDEOIL")["min_risk_reward"] == 5.0
        assert app.get_sr_live_params("NIFTY")["min_risk_reward"] == app.ENGINE_PARAM_SPECS["sr"]["min_risk_reward"]["default"]
