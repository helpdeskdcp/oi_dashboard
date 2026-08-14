#!/usr/bin/env python3
"""
Brahma Autonomous Trading Intelligence (BATI) -- Multi-Symbol OI Scalping
Dashboard, Flask + SocketIO
=============================================================================
Live browser dashboard, dark/orange theme, WebSocket push updates every
REFRESH_INTERVAL sec, chart of LTP/PCR over time, bias-change alerts,
symbol switcher (NSE indices + MCX commodities).

Angel One SmartAPI = PRIMARY data source for everything (reliable, authenticated).
NSE website        = best-effort secondary for NSE indices only (Akamai usually
                      blocks it -- failures are logged, never block the app).

SUPPORTED SYMBOLS
-----------------
NSE indices (options available): NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX
NSE index, spot-only (no option chain): INDIA VIX
MCX commodities (options available): CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI,
                                      GOLD, GOLDM, SILVER, SILVERM

NOT AVAILABLE via Angel One SmartAPI (shown disabled in the dropdown):
DOW JONES (US index -- Indian brokers don't provide US market access)
GIFT NIFTY (trades on NSE IX / Gift City -- not on standard retail Angel One API)

RUN
---
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # fill Angel One credentials
python3 app.py
Open: http://<vps-ip>:5050  (or http://127.0.0.1:5050 on Termux itself)

IMPORTANT -- verify before relying on it live:
MCX strike steps (STRIKE_STEP below) are best-effort defaults. Contract specs
change; if ATM strikes don't resolve (option table stays empty for a commodity),
open instrument_master.json, find that commodity's OPTFUT rows, and correct the
step to match the real strike interval.
"""

import os
import re
import secrets
import threading
import sys
import time
import json
import sqlite3
import logging
import logging.handlers
import datetime as dt
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))   # explicit, fixed IST offset -- the "ts" strings in cycles/snapshots are IST wall-clock-time (matching NSE/MCX trading hours), so this ensures correct UTC-epoch conversion regardless of the server OS's own timezone configuration
from collections import deque
from dataclasses import dataclass

import requests
import pyotp
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, g, abort
from flask_socketio import SocketIO, join_room, leave_room

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

load_dotenv(override=True)   # .env wins over any pre-existing shell/bashrc env vars (avoids cross-project collisions, e.g. DB_PATH from other trading bots on the same VPS)

# auth/billing MUST be imported AFTER load_dotenv() -- both read DB_PATH at
# their own module level (os.getenv("DB_PATH", ...)), same as every other
# local module below. Importing them before load_dotenv() ran (a real bug,
# found live: this VPS has a stray DB_PATH exported for a DIFFERENT project,
# which both modules silently picked up instead of this app's oi_history.db,
# breaking every wallet operation with "no such column" errors against the
# wrong database) would resolve DB_PATH from whatever was in the process
# environment BEFORE this file's own .env override took effect.
import auth
import billing
from agents import audit_log as agent_audit_log
from agents import config as agents_config
from agents import event_bus as agent_event_bus
from agents.intelligence_alerts import api as intelligence_alerts_api
from agents.intelligence_alerts import cooldown as intelligence_alerts_cooldown
from agents.intelligence_alerts import dedup_store as intelligence_alerts_dedup_store
from agents.intelligence_alerts import rate_limiter as intelligence_alerts_rate_limiter
from agents.intelligence_alerts import retry_tracker as intelligence_alerts_retry_tracker
from agents.intelligence_alerts import rules as intelligence_alerts_rules
from agents.intelligence_alerts import threshold_store as intelligence_alerts_threshold_store
from agents.intelligence_alerts import store as intelligence_alerts_store
from agents.intelligence_history import api as intelligence_history_api
from agents.intelligence_history import store as intelligence_history_store
from agents.ops import diagnostics as ops_diagnostics
from agents.ops import event_log as ops_event_log
from agents.ops import models as ops_models
from agents.risk_manager import api as risk_api
from agents.risk_manager import risk_store as agent_risk_store
from agents.runtime import lifecycle as runtime_lifecycle
from agents.runtime import policy_engine as runtime_policy_engine
from agents.runtime import runtime_store as agent_runtime_store
from agents.runtime import scheduling_control as runtime_scheduling_control
from agents.runtime import trading_mode as runtime_trading_mode
from agents.shadow_mode import api as shadow_api
from agents.shadow_mode import store as shadow_store
from agents.sys_admin import api as sysadmin_api
from agents.sys_admin import sysadmin_store as agent_sysadmin_store
from agents.runtime import market_session as agents_market_session
from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import candle_recorder
from agents.trading_intelligence import paper_trade_diagnostics
from agents.trading_intelligence import structure_overlay
from agents.trading_intelligence import monitoring_center
from agents.trading_intelligence import structure_tuning
from agents.trading_intelligence import ti_store
from agents.trading_intelligence import virtual_trailing
from agents.trading_supervisor import supervision_store as agent_supervision_store

import expiry_intelligence
import intelligence_orchestrator
import mcx_session_config

REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "1"))  # 2026-07-31: sped up from 7s to 1s for the ACTIVE symbol only, per request. Background symbols remain at BACKGROUND_REFRESH_SECONDS (45s) -- unchanged, to keep total API-call volume manageable. Monitor logs for increased rate-limit warnings; revert to a higher value if they become frequent.
STRIKES_EACH_SIDE = int(os.getenv("STRIKES_EACH_SIDE", "4"))
PORT = int(os.getenv("PORT", "5050"))
DEFAULT_SYMBOL = os.getenv("SYMBOL", "NIFTY").upper()

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Milestone 18: separate public "IDaddy Scalping Signals" channel -- same bot
# (TELEGRAM_BOT_TOKEN), different destination, so the personal admin chat
# above and the public signals channel are never accidentally conflated.
# Empty by default (unset until an admin adds the channel's @username or
# numeric -100... chat_id here); send_telegram_channel() below no-ops
# exactly like send_telegram() already does when its own vars are unset.
TELEGRAM_SIGNALS_CHANNEL_ID = os.getenv("TELEGRAM_SIGNALS_CHANNEL_ID", "")

# Accounts / subscriptions -- shown to subscribers on the low-balance banner
# and the trial/subscription-expired page (see auth.py, billing.py, and the
# /register /login /admin/users routes further down).
SUPPORT_PHONE_NUMBER = os.getenv("SUPPORT_PHONE_NUMBER", "")
ADMIN_BOOTSTRAP_USERNAME = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "")
ADMIN_BOOTSTRAP_EMAIL = os.getenv("ADMIN_BOOTSTRAP_EMAIL", "") or None
ADMIN_BOOTSTRAP_PASSWORD = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "")

# -- "Sign in with Google" (Firebase Auth, client-side popup SDK) -- FIREBASE_API_KEY/
# FIREBASE_AUTH_DOMAIN/FIREBASE_APP_ID are public-by-design (embedded in the page,
# same as any client-side Firebase config) and identify the project, not a secret
# credential -- the actual trust boundary is server-side ID-token verification
# (see api_auth_google), not these values. FIREBASE_PROJECT_ID is derived from
# FIREBASE_AUTH_DOMAIN ("<project-id>.firebaseapp.com") rather than duplicated in
# .env, since Firebase's own convention guarantees that relationship.
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "")
FIREBASE_AUTH_DOMAIN = os.getenv("FIREBASE_AUTH_DOMAIN", "")
FIREBASE_APP_ID = os.getenv("FIREBASE_APP_ID", "")
FIREBASE_PROJECT_ID = FIREBASE_AUTH_DOMAIN.split(".")[0] if FIREBASE_AUTH_DOMAIN else ""
GOOGLE_SIGNIN_ENABLED = bool(FIREBASE_API_KEY and FIREBASE_AUTH_DOMAIN and FIREBASE_APP_ID)

# -- Smart Chatbot (Groq, OpenAI-compatible chat-completions API) -- server-side
# key, unlike the earlier design where each user pasted their own OpenAI key into
# browser localStorage. GROQ_CHAT_MODEL is swappable via .env without a code change.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")

INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENT_MASTER_CACHE = "instrument_master.json"

MAX_HISTORY_POINTS = 200
MAX_ALERTS = 30

# --- Signal Engine / Paper Trading config -----------------------------------
SIGNAL_CONFIDENCE_THRESHOLD = int(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "60"))   # 0-100, min to auto-enter
VOLUME_EXPANSION_MULT = float(os.getenv("VOLUME_EXPANSION_MULT", "1.5"))   # Fake-Breakout-Filter: current volume must be this many times the recent average -- found to be the dominant entry-blocking gate (~98% of blocks, 2026-07-21), live-tunable via /dev-settings
TARGET_DELTA_APPROX = float(os.getenv("TARGET_DELTA_APPROX", "0.55"))               # ATM option delta approximation
SL_PERCENT = float(os.getenv("SL_PERCENT", "0.35"))                                 # stop loss = 35% of premium (MAX risk cap)
MAX_SL_PERCENT = float(os.getenv("MAX_SL_PERCENT", "0.05"))                         # SL revised 2026-07-21: adaptive swing-low/high stop, hard-capped at 5% max risk of entry premium (replaces the old 15%-35% flat clamp)
MIN_TARGET_PERCENT = float(os.getenv("MIN_TARGET_PERCENT", "0.15"))                 # target floor = 15% of premium
# Structural quality gate: only reward/allow signals near a REAL level (PDH/PDL,
# swing high/low, opening range, pivots) -- not just any nearby OI wall. This is
# what turns "a signal fires constantly" into "only the setups that matter."
STRUCTURAL_PROXIMITY_ATR_MULT = float(os.getenv("STRUCTURAL_PROXIMITY_ATR_MULT", "0.5"))
STRUCTURAL_BONUS = int(os.getenv("STRUCTURAL_BONUS", "20"))
STRUCTURAL_PENALTY = int(os.getenv("STRUCTURAL_PENALTY", "30"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "30"))                         # force time-exit
# Minimum gap between retry-login attempts when the client is down (e.g. rate-
# limited at startup) -- prevents 14 concurrent symbol threads from hammering
# a still-rate-limited login endpoint every single 7-45s cycle.
LOGIN_RETRY_COOLDOWN_SECONDS = int(os.getenv("LOGIN_RETRY_COOLDOWN_SECONDS", "30"))

# --- Trailing SL: 2-stage (breakeven, then trail behind peak) ---
TRAILING_SL_ENABLED = os.getenv("TRAILING_SL_ENABLED", "true").lower() == "true"
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.3"))   # 30% of the way to target -> SL to breakeven
TRAIL_TRIGGER_PCT = float(os.getenv("TRAIL_TRIGGER_PCT", "0.6"))           # 60% of the way to target -> start trailing
TRAIL_GIVEBACK_PCT = float(os.getenv("TRAIL_GIVEBACK_PCT", "0.3"))         # give back only 30% of peak gain

# --- Phase 3: unified paper_orders (Manual + AI Auto) -----------------------
INTRADAY_SQUAREOFF_BUFFER_MINUTES = int(os.getenv("INTRADAY_SQUAREOFF_BUFFER_MINUTES", "5"))   # BRACKET/COVER orders force-close this many minutes before the symbol's close -- comfortably above the worst-case ~45s background cycle interval, so a forced square-off can never sail past close unclosed
MAX_AUTO_TRADE_FANOUT_USERS = int(os.getenv("MAX_AUTO_TRADE_FANOUT_USERS", "200"))   # soft cap on a single fan-out event (log + skip beyond N) -- SQLite is single-writer, so N enrolled users means N serialized BEGIN IMMEDIATE transactions; fine at this scale, a future batched-transaction optimization is the seam if this ever becomes a bottleneck

# --- Smart stagnant-exit: cut early if the trade genuinely isn't moving, instead
#     of always waiting the full MAX_HOLD_MINUTES and bleeding theta decay ---
STAGNANT_EXIT_ENABLED = os.getenv("STAGNANT_EXIT_ENABLED", "true").lower() == "true"
STAGNANT_EXIT_MINUTES = int(os.getenv("STAGNANT_EXIT_MINUTES", "15"))              # check-point, half of default hold time
STAGNANT_EXIT_THRESHOLD_PCT = float(os.getenv("STAGNANT_EXIT_THRESHOLD_PCT", "0.1"))  # <10% of target distance moved = stagnant
PAPER_TRADE_LOT_QTY = int(os.getenv("PAPER_TRADE_LOT_QTY", "1"))                    # in lots, for P&L display
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "true").lower() == "true"

# --- Fake-signal filter (reduces whipsaws / noise trades) -------------------
MIN_OI_CHANGE_THRESHOLD = int(os.getenv("MIN_OI_CHANGE_THRESHOLD", "500"))        # ignore OI chg smaller than this
# Percentage-based version -- scales correctly across instruments of very different
# OI size (500 contracts is noise for NIFTY, might be most of NATGASMINI's OI).
# Used when previous-cycle OI is known; falls back to MIN_OI_CHANGE_THRESHOLD (absolute)
# on the very first cycle after a restart, when there's no previous OI to compare against.
MIN_OI_CHANGE_PERCENT = float(os.getenv("MIN_OI_CHANGE_PERCENT", "2.0"))
# On expiry day, theta decay alone can move premiums several % with zero real
# buildup/unwinding happening -- require a bigger price move before trusting
# price-direction as a genuine signal (see classify_buildup's min_price_chg_pct).
EXPIRY_DAY_MIN_PRICE_CHG_PCT = float(os.getenv("EXPIRY_DAY_MIN_PRICE_CHG_PCT", "3.0"))
BIAS_PERSISTENCE_SECONDS = int(os.getenv("BIAS_PERSISTENCE_SECONDS", "60"))        # bias must hold this many real seconds
NEUTRAL_GRACE_SECONDS = int(os.getenv("NEUTRAL_GRACE_SECONDS", "20"))              # tolerate a brief NEUTRAL blip without resetting the streak
COOLDOWN_MINUTES_AFTER_SL = int(os.getenv("COOLDOWN_MINUTES_AFTER_SL", "10"))   # pause new entries after a stop-loss
TIME_EXIT_COOLDOWN_THRESHOLD = int(os.getenv("TIME_EXIT_COOLDOWN_THRESHOLD", "2"))   # N consecutive time-exits before cooling down
COOLDOWN_MINUTES_AFTER_TIMEOUT = int(os.getenv("COOLDOWN_MINUTES_AFTER_TIMEOUT", "15"))   # pause after repeated non-resolving trades (flat market)

DB_PATH = os.getenv("DB_PATH", "oi_history.db")

# --- Market-hours awareness -- pauses ALL Angel One / NSE calls when markets
#     are closed (weekends, before/after session) to save API credits. ---
CLOSED_SLEEP_SECONDS = int(os.getenv("CLOSED_SLEEP_SECONDS", "300"))   # how often to re-check while closed

# All symbols are tracked simultaneously (for backtest data coverage), but only
# the actively-VIEWED symbol needs live scalping-speed refresh. Everything else
# refreshes slower to stay well within Angel One's rate limits.
BACKGROUND_REFRESH_SECONDS = int(os.getenv("BACKGROUND_REFRESH_SECONDS", "45"))
# Startup stagger between symbols -- was 0.7s, widened because each symbol's
# FIRST cycle also does a heavy candle-history fetch (5 days of 5-min data),
# not just a quote poll. 14 symbols x 0.7s wasn't enough gap to avoid a
# rate-limit burst on process start; wider spacing trades a longer startup
# ramp-up (14 symbols x 3s = ~42s to all be live) for a clean burst-free start.
SYMBOL_STARTUP_STAGGER_SECONDS = float(os.getenv("SYMBOL_STARTUP_STAGGER_SECONDS", "5.0"))

# --- Dev mode: keep fetching (throttled) even when market is closed, so you
#     can develop/test the UI on weekends using Friday's last-known data. ---
DEV_MODE_WHEN_CLOSED = os.getenv("DEV_MODE_WHEN_CLOSED", "false").lower() == "true"
DEV_MODE_REFRESH_SECONDS = int(os.getenv("DEV_MODE_REFRESH_SECONDS", "60"))
IST_OFFSET = dt.timedelta(hours=5, minutes=30)

# (open_hour, open_min, close_hour, close_min) in IST, per symbol type.
# Effective 2026-08-03 NSE/MCX timing revision.
#
# NSE:
# - "index_option": Equity F&O (index options/futures, e.g. NIFTY/BANKNIFTY/
#   SENSEX) -- 09:15-15:40 (extended from the prior 15:30 close).
# - "index_spot": a cash/normal-session index value with no option chain of
#   its own (INDIA VIX) -- tracks the plain cash session, 09:15-15:30,
#   unaffected by the F&O close-time extension.
# - "fno_cash_stock": F&O-eligible cash stocks -- continuous trading ends
#   15:15, followed by a Closing Auction Session (CAS) until 15:35. This app
#   does not currently track any individual stock symbols (only indices and
#   MCX commodities), so nothing maps to this type today; the 15:15
#   continuous-close boundary is recorded here for is_market_open()'s
#   boolean check. CAS itself (call-auction-only, not continuous trading) is
#   NOT separately modeled -- there is no CAS-aware behavior anywhere in
#   this app to gate, since no symbol currently uses this type.
# - "non_fno_stock": non-F&O cash stocks -- unchanged normal session,
#   09:15-15:30. Also currently unused (no stock symbols tracked).
#
# MCX:
# - "commodity_agri": agricultural commodities -- 09:00-17:00. (Select
#   global agri commodities like cotton trade until 21:00 per the exchange
#   circular; not modeled separately since no agri commodity is currently
#   tracked by this app.)
# - "commodity_nonagri": non-agricultural commodities (metals/energy/
#   bullion -- every MCX symbol this app currently tracks: CRUDEOIL(M),
#   NATURALGAS/NATGASMINI, GOLD(M), SILVER(M)) -- 09:00 open, close shifts
#   seasonally per _mcx_nonagri_close() below (23:30 IST standard, 23:55
#   IST during the DST-linked extended window). The close value in this
#   dict is a placeholder, never read directly -- see
#   _resolve_market_hours().
MARKET_HOURS = {
    "index_option":      (9, 15, 15, 40),
    "index_spot":        (9, 15, 15, 30),
    "fno_cash_stock":     (9, 15, 15, 15),
    "non_fno_stock":      (9, 15, 15, 30),
    "commodity_agri":     (9, 0, 17, 0),
    "commodity_nonagri":  (9, 0, 23, 30),
}

# Any MCX commodity, agri or non-agri -- used wherever code needs "is this
# symbol MCX" (broker exchange-segment resolution, expiry-day detection)
# rather than a specific session-hours lookup.
COMMODITY_TYPES = ("commodity_agri", "commodity_nonagri")


def now_ist():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST_OFFSET


def _nth_weekday_of_month(year, month, weekday, n):
    """The n-th occurrence of `weekday` (0=Monday..6=Sunday) in `month`/`year`."""
    d = dt.date(year, month, 1)
    days_ahead = (weekday - d.weekday()) % 7
    d += dt.timedelta(days=days_ahead + 7 * (n - 1))
    return dt.datetime(d.year, d.month, d.day)


def _mcx_nonagri_close(now):
    """MCX's non-agricultural (metals/energy/bullion) session close shifts
    with the seasonal, DST-linked schedule MCX's own periodic circular
    follows -- the actual close times themselves come from
    mcx_session_config.py (MCX_NON_AGRI_SUMMER_CLOSE/MCX_NON_AGRI_WINTER_CLOSE),
    the single source of truth for them, not hardcoded here.

    CAVEAT: MCX sets the exact CUTOVER DATES itself, circular by
    circular, and they can shift year to year -- the window below
    approximates them using the standard US DST window (2nd Sunday of
    March through the 1st Sunday of November); see mcx_session_config.
    MCX_DST_MODE and warn_if_approximate() for the operator-facing
    warning this carries until that's verified against the real
    exchange circular."""
    year = now.year
    dst_start = _nth_weekday_of_month(year, 3, 6, 2)    # 2nd Sunday of March
    dst_end = _nth_weekday_of_month(year, 11, 6, 1)     # 1st Sunday of November
    if dst_start.date() <= now.date() < dst_end.date():
        return mcx_session_config.summer_close()
    return mcx_session_config.winter_close()


def _resolve_market_hours(cfg, now):
    """(open_hour, open_min, close_hour, close_min) for `cfg` at `now` --
    a plain MARKET_HOURS lookup for every type except MCX non-agri
    commodities, whose close time is date-dependent (see
    _mcx_nonagri_close()). Shared by is_market_open() and the intraday
    auto-square-off buffer calculation so both always agree on the
    session's real close time."""
    oh, om, ch, cm = MARKET_HOURS.get(cfg["type"], (9, 15, 15, 30))
    if cfg["type"] == "commodity_nonagri":
        ch, cm = _mcx_nonagri_close(now)
    return oh, om, ch, cm


def is_market_open(cfg):
    now = now_ist()
    if now.weekday() >= 5:   # 5=Saturday, 6=Sunday
        return False, "Weekend"
    oh, om, ch, cm = _resolve_market_hours(cfg, now)
    open_t = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if open_t <= now <= close_t:
        return True, ""
    return False, "Outside trading hours"

# --- Symbol registry -------------------------------------------------------
# type: "index_option" (NSE index w/ options), "index_spot" (no option chain),
#       "commodity_agri" / "commodity_nonagri" (MCX, underlying = nearest
#       futures contract -- see MARKET_HOURS's own docstring-style comment
#       above for what distinguishes the two and COMMODITY_TYPES for
#       checking "is this symbol MCX" regardless of which)
SYMBOLS = {
    "NIFTY":       {"label": "NIFTY 50",      "group": "NSE Index",  "type": "index_option",
                     "exch": "NSE", "step": 50,  "spot_token": "99926000", "options_exch_seg": "NFO"},
    "BANKNIFTY":   {"label": "BANK NIFTY",    "group": "NSE Index",  "type": "index_option",
                     "exch": "NSE", "step": 100, "spot_token": "99926009", "options_exch_seg": "NFO"},
    "FINNIFTY":    {"label": "FIN NIFTY",     "group": "NSE Index",  "type": "index_option",
                     "exch": "NSE", "step": 50,  "spot_token": "99926037", "options_exch_seg": "NFO"},
    "MIDCPNIFTY":  {"label": "MIDCAP NIFTY",  "group": "NSE Index",  "type": "index_option",
                     "exch": "NSE", "step": 25,  "spot_token": None, "options_exch_seg": "NFO"},   # spot resolved dynamically
    "SENSEX":      {"label": "SENSEX",        "group": "NSE Index",  "type": "index_option",
                     "exch": "BSE", "step": 100, "spot_token": None, "options_exch_seg": "BFO"},   # BSE F&O segment, NOT NFO -- spot resolved dynamically
    "INDIA VIX":   {"label": "INDIA VIX",     "group": "NSE Index",  "type": "index_spot",
                     "exch": "NSE", "step": None, "spot_token": "99926017", "options_exch_seg": None},

    "CRUDEOIL":    {"label": "CRUDEOIL",      "group": "MCX Commodity", "type": "commodity_nonagri", "step": 50},
    "CRUDEOILM":   {"label": "CRUDEOIL MINI", "group": "MCX Commodity", "type": "commodity_nonagri", "step": 50},
    "NATURALGAS":  {"label": "NATURALGAS",    "group": "MCX Commodity", "type": "commodity_nonagri", "step": 10},
    "NATGASMINI":  {"label": "NATGAS MINI",   "group": "MCX Commodity", "type": "commodity_nonagri", "step": 10},
    "GOLD":        {"label": "GOLD",          "group": "MCX Commodity", "type": "commodity_nonagri", "step": 100},
    "GOLDM":       {"label": "GOLD MINI",     "group": "MCX Commodity", "type": "commodity_nonagri", "step": 100},
    "SILVER":      {"label": "SILVER",        "group": "MCX Commodity", "type": "commodity_nonagri", "step": 100},
    "SILVERM":     {"label": "SILVER MINI",   "group": "MCX Commodity", "type": "commodity_nonagri", "step": 100},
}

# Longest-key-first so a broker trading-symbol like "GOLDM24JUL..." matches
# "GOLDM" before the shorter "GOLD" -- plain dict-declaration-order iteration
# would match "GOLD" first for EVERY GOLDM/SILVERM/CRUDEOILM position, silently
# analyzing them against the wrong base symbol's option chain/ATM.
_SYMBOL_KEYS_BY_LEN_DESC = sorted(SYMBOLS.keys(), key=len, reverse=True)


def match_symbol_prefix(trading_symbol):
    """Maps a broker trading-symbol (e.g. 'GOLDM24JUL5500CE') to one of our
    tracked SYMBOLS keys via longest-prefix-first matching. Returns None if
    no tracked symbol matches."""
    if not trading_symbol:
        return None
    upper = trading_symbol.upper()
    return next((s for s in _SYMBOL_KEYS_BY_LEN_DESC if upper.startswith(s)), None)


# Lot sizes for auto Qty-calculation on the Manual Trading page (Qty entered
# = number of LOTS, actual quantity = lots x lot_size). SOURCED from public
# exchange-revision reporting as of July 2026 -- NSE/MCX periodically revise
# these (e.g. NIFTY dropped from 75 to 65 in Jan 2026), so this is a
# best-known-value reference, NOT an authoritative live feed. VERIFY against
# your broker/exchange before using for anything beyond paper-trading.
# Entries marked "verify" have genuine unit-convention ambiguity (e.g. GOLD's
# lot is sometimes quoted as "1kg" vs "100 units of 10g") -- confirm before trusting.
LOT_SIZES = {
    "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "SENSEX": 20,
    "INDIA VIX": 1,   # spot-only reference, not directly tradeable via lots in this system
    "CRUDEOIL": 100, "CRUDEOILM": 10,
    "NATURALGAS": 1250, "NATGASMINI": 250,
    "GOLD": 100, "GOLDM": 10,      # VERIFY: unit-convention ambiguity (grams vs kg quoting)
    "SILVER": 30, "SILVERM": 5,
}

# Shown in the dropdown as disabled -- honestly not available via Angel One SmartAPI.
UNAVAILABLE_SYMBOLS = [
    {"label": "DOW JONES", "reason": "US index -- not accessible via an Indian broker API"},
    {"label": "GIFT NIFTY", "reason": "Trades on NSE IX (Gift City) -- not on standard Angel One retail API"},
]

if DEFAULT_SYMBOL not in SYMBOLS:
    DEFAULT_SYMBOL = "NIFTY"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.handlers.RotatingFileHandler("flask_dashboard.log", maxBytes=5_000_000, backupCount=5)],
)
log = logging.getLogger("oi_dashboard")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True   # templates re-read from disk on every request -- a
                                              # template-only deploy (like dashboard.html) takes
                                              # effect immediately, no app restart needed
_flask_secret = os.getenv("FLASK_SECRET")
if not _flask_secret:
    _flask_secret = os.urandom(32).hex()
    logging.getLogger("oi_dashboard").warning(
        "FLASK_SECRET not set in .env -- generated a random secret for this process "
        "(sessions won't survive a restart). Set FLASK_SECRET to a fixed value in production."
    )
app.config["SECRET_KEY"] = _flask_secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only set this true once the dashboard is genuinely served over HTTPS (e.g.
    # behind an Nginx + Let's Encrypt reverse proxy) -- if it's true while the
    # site is plain HTTP, the browser silently refuses to send the session
    # cookie back and login appears to "not work" with no visible error.
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=7),
)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Old single-shared-credential HTTP Basic Auth has been REPLACED by real
# per-user accounts + session login (see auth.py, and the /login, /register,
# /admin/users routes below) -- DASHBOARD_USERNAME/DASHBOARD_PASSWORD in .env
# are no longer read anywhere; left in place harmlessly for anyone who still
# has them set. Route-level access control now lives entirely in the
# @auth.login_required / @auth.roles_required / @auth.subscription_required
# decorators applied to each route further down this file, enforced by a
# fail-closed startup self-check (_verify_all_routes_protected, called from
# the autostart block at the bottom of this file) that refuses to boot if any
# route is missing one of those decorators.
app.jinja_env.globals["csrf_token"] = auth.get_csrf_token


@app.before_request
def _load_logged_in_user():
    """Populates flask.g.user fresh from SQLite on EVERY request (never
    trusts session-cached role/wallet/subscription data) -- so an admin
    suspending a user, changing their role, or editing their wallet takes
    effect on that user's very next request, not after they log out and back
    in. session[] only ever stores the numeric user_id."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None or row["is_suspended"]:
        session.pop("user_id", None)
        return
    g.user = row


@app.before_request
def _csrf_guard():
    # login_page/register_page ARE covered too (not exempt) -- rendering the
    # GET form mints a csrf_token into the (pre-auth, cookie-based) session
    # via the Jinja global below, so the POST submission carries a valid
    # token same as any authenticated form. Only static assets are exempt.
    if request.endpoint in ("static",) or request.endpoint is None:
        return
    auth.csrf_guard()


@app.context_processor
def inject_current_user():
    u = getattr(g, "user", None)
    return {
        "current_user": {
            "logged_in": u is not None,
            "id": u["id"] if u else None,
            "role": u["role"] if u else None,
            "email": u["email"] if u else None,
            "username": u["username"] if u else None,
            "wallet_balance": u["wallet_balance"] if u else None,
            "is_full_view": bool(u) and u["role"] in ("admin", "developer"),
            "is_admin": bool(u) and u["role"] == "admin",
        },
        "support_phone": SUPPORT_PHONE_NUMBER,
        "trial_days": auth.TRIAL_DAYS,
        "wallet_low_threshold": billing.WALLET_LOW_BALANCE_THRESHOLD,
        "google_signin_enabled": GOOGLE_SIGNIN_ENABLED,
        "firebase_api_key": FIREBASE_API_KEY,
        "firebase_auth_domain": FIREBASE_AUTH_DOMAIN,
        "firebase_app_id": FIREBASE_APP_ID,
        "firebase_project_id": FIREBASE_PROJECT_ID,
    }


PUBLIC_ENDPOINTS = {"static", "register_page", "verify_email_page", "login_page", "api_auth_google"}


def _verify_all_routes_protected():
    """Fail-closed startup self-check: every route must be either explicitly
    public (PUBLIC_ENDPOINTS) or decorator-tagged with __auth_protected__ by
    auth.login_required/roles_required/subscription_required. An
    accidentally-unprotected route becomes a boot failure here, not a silent
    access-control gap discovered later."""
    unprotected = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        if not getattr(view, "__auth_protected__", False):
            unprotected.append(rule.endpoint)
    if unprotected:
        raise RuntimeError(
            "Route protection self-check FAILED -- these endpoints have no "
            "auth decorator applied and are NOT in PUBLIC_ENDPOINTS: "
            + ", ".join(sorted(set(unprotected)))
        )
    log.info("Route protection self-check passed -- every route is either public or access-controlled.")


def parse_expiry(expiry_str: str) -> dt.date:
    """Angel One expiry format is 'DDMMMYYYY' (e.g. '26AUG2026') -- a STRING sort on
    this is wrong (e.g. '24NOV2026' < '26AUG2026' lexicographically, even though
    Aug is chronologically earlier). Always sort using this parsed date, never the raw string."""
    try:
        return dt.datetime.strptime(expiry_str, "%d%b%Y").date()
    except Exception:
        return dt.date.max   # unparseable -> sinks to the end, never picked as "nearest"


# ----------------------------------------------------------------------------
# NSE (secondary cross-check -- now working via the GetQuoteApi endpoint
# discovered from NSE's own frontend JS bundle; see nse_fetcher.py for the
# full production client. Kept as secondary, NOT primary: this is an
# undocumented internal endpoint NSE could change without notice, whereas
# Angel One SmartAPI is a stable, documented broker API.)
# ----------------------------------------------------------------------------

from nse_fetcher import NSEFetcher as _NSEFetcherImpl, normalize_nse_chain, NSECircuitBreakerOpen
from bse_fetcher import BSEOptionChainFetcher, normalize_bse_chain
import institutional_levels
from market_structure import build_market_structure
from sr_probability_engine import (
    build_sr_probability_table, advance_level_state, check_structural_trigger,
    check_candle_close_confirmation, compute_staged_underlying_targets,
    advance_active_level, score_strike_candidates,
    compute_premium_entry_trigger, compute_premium_momentum, check_premium_momentum_confirmed,
    compute_dynamic_targets_sl, validate_risk_reward,
    classify_price_structure, compute_institutional_entry_score,
    compute_volume_expansion, fake_breakout_filter,
)
from engine_v2 import build_v2_probability_table, compute_v2_levels
from candlestick_patterns import detect_patterns
from scalping_engine import generate_scalp_signal, SCALP_MAX_HOLD_MINUTES, SCALP_COOLDOWN_MINUTES_AFTER_SL
from sr_engine_v3 import (
    generate_v3_signal, validate_previous_day_levels, should_pause_time_exit,
    learn_adaptive_weights, V3_DEFAULT_FACTOR_WEIGHTS,
    V3_MIN_RISK_REWARD, V3_CONFIDENCE_TRADE_THRESHOLD, V3_MIN_TARGET_PCT, V3_MAX_SL_PCT,
)
from dynamic_sr_engine import evaluate as evaluate_dynamic_sr, MIN_TRADEABLE_CONFIDENCE
import exit_engine_v4
try:
    from chatgpt_commentary import get_commentary, COMMENTARY_ENABLED, COMMENTARY_REFRESH_SECONDS
except ImportError:
    COMMENTARY_ENABLED = False
    COMMENTARY_REFRESH_SECONDS = 120
    def get_commentary(*args, **kwargs):
        return None

try:
    from ollama_service import get_ai_insight, OLLAMA_ENABLED, OLLAMA_REFRESH_SECONDS, health_check as ollama_health_check
except ImportError:
    OLLAMA_ENABLED = False
    OLLAMA_REFRESH_SECONDS = 300
    def get_ai_insight(*args, **kwargs):
        return None
    def ollama_health_check():
        return False


class NSEFetcher:
    """Thin adapter so the rest of app.py doesn't need to change: same
    get_option_chain(symbol) call site, but now backed by the real client."""

    def __init__(self):
        self._impl = _NSEFetcherImpl(snapshot_dir=os.getenv("NSE_SNAPSHOT_DIR") or None)

    def get_option_chain(self, symbol: str):
        if symbol not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            return None   # SENSEX is BSE, not on this endpoint; commodities never are
        try:
            chain_json, expiry = self._impl.get_option_chain(symbol)
            return chain_json
        except NSECircuitBreakerOpen as e:
            log.warning(f"NSE circuit breaker open: {e}")
            return None
        except Exception as e:
            log.warning(f"NSE fetch failed for {symbol}: {e}")
            return None

    def get_cross_check(self, symbol: str, wanted_strikes: list):
        """Full normalized NSE data for side-by-side comparison against Angel One.
        Returns None if NSE is unavailable this cycle -- callers must handle that."""
        if symbol not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            return None
        try:
            chain_json, expiry = self._impl.get_option_chain(symbol)
            rows_by_strike, underlying = normalize_nse_chain(chain_json, StrikeRow, classify_buildup, wanted_strikes)
            rows = [rows_by_strike[s] for s in wanted_strikes if s in rows_by_strike]
            if not rows:
                return None
            pcr = calc_pcr(rows)
            total_ce_oi = sum(r.ce_oi for r in rows)
            total_pe_oi = sum(r.pe_oi for r in rows)
            return {"underlying": underlying, "pcr": pcr, "total_ce_oi": total_ce_oi,
                    "total_pe_oi": total_pe_oi, "expiry": expiry, "rows_by_strike": rows_by_strike}
        except Exception as e:
            log.warning(f"NSE cross-check fetch failed for {symbol}: {e}")
            return None


class BSEFetcher:
    """Same interface as NSEFetcher, backed by bse_fetcher.py -- used for SENSEX
    and other BSE-listed indices, since NSE's endpoint has no BSE data at all."""

    def __init__(self):
        self._impl = BSEOptionChainFetcher()

    def get_cross_check(self, symbol: str, strikes_each_side: int = 4):
        if symbol not in self._impl.SYMBOLS:
            return None
        try:
            result = self._impl.get_option_chain(symbol, strikes_each_side=strikes_each_side)
            rows_by_strike = normalize_bse_chain(result, StrikeRow)
            rows = list(rows_by_strike.values())
            if not rows:
                return None
            pcr = calc_pcr(rows)
            total_ce_oi = sum(r.ce_oi for r in rows)
            total_pe_oi = sum(r.pe_oi for r in rows)
            return {"underlying": result["spot"], "pcr": pcr, "total_ce_oi": total_ce_oi,
                    "total_pe_oi": total_pe_oi, "expiry": result["expiry"], "rows_by_strike": rows_by_strike}
        except Exception as e:
            log.warning(f"BSE cross-check fetch failed for {symbol}: {e}")
            return None


# ----------------------------------------------------------------------------
# ANGEL ONE (primary, all symbols)
# ----------------------------------------------------------------------------

class AngelOneFetcher:
    SESSION_REFRESH_SECONDS = int(os.getenv("ANGEL_SESSION_REFRESH_SECONDS", str(6 * 3600)))   # proactive relogin every N sec
    SESSION_ERROR_HINTS = ("invalid token", "session expired", "ag8001", "ag8002", "tokenexception",
                            "unauthorized", "please login again")
    # Angel One's own rate-limit message is literally "Access denied because of
    # exceeding access rate" -- this is NOT a session problem, relogging in won't
    # help and just adds more load. It needs a short backoff + retry instead.
    RATE_LIMIT_HINTS = ("exceeding access rate", "access denied", "rate limit", "too many requests", "ag8002")

    def __init__(self):
        self.client = None
        self.instruments = []
        self._last_login_time = None
        self._last_login_attempt = None
        self._login_lock = threading.Lock()
        self._login()
        self._load_instrument_master()
        self._spot_token_cache = {}
        self._future_token_cache = {}

    def _login(self):
        if not all([ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET]):
            log.warning("Angel One credentials missing in .env -> live data will not work.")
            return
        try:
            from SmartApi import SmartConnect
            self.client = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            session = self.client.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)
            if not session or not session.get("status"):
                raise RuntimeError(session)
            self._last_login_time = time.time()
            log.info("Angel One SmartAPI login OK.")
        except Exception as e:
            log.error(f"Angel One login failed: {e}")
            self.client = None

    def _ensure_session_fresh(self):
        """Proactive relogin -- Angel One sessions can go stale over a long trading
        day even without an explicit error; refresh on a timer rather than waiting
        to hit a failure.

        CRITICAL FIX (found live, 2026-07-15): if the login itself gets rate-limited
        (e.g. at market open when the API server is busiest), self.client stays None
        forever -- every data-fetch method used to just silently short-circuit on
        "if not self.client: return ..." without EVER retrying, permanently breaking
        the whole app until a manual restart (this happened live: broken 09:00 to
        13:11, over 4 hours, completely silent). Now every data call goes through
        this method first, which retries login if needed.

        LOGIN_RETRY_COOLDOWN_SECONDS + a lock prevent 14 concurrent symbol threads
        from hammering the login endpoint simultaneously every cycle while it's
        still rate-limited -- that would make the rate-limit situation worse, not
        better."""
        if self.client is not None and self._last_login_time is not None:
            if time.time() - self._last_login_time > self.SESSION_REFRESH_SECONDS:
                with self._login_lock:
                    if self.client is not None and time.time() - self._last_login_time > self.SESSION_REFRESH_SECONDS:
                        log.info("Angel One session refresh interval reached -- relogging in proactively.")
                        self._login()
            return

        # client is None -- need a (re)login, but don't hammer it every cycle
        with self._login_lock:
            if self.client is not None:
                return   # another thread already relogged in while we waited for the lock
            if self._last_login_attempt and (time.time() - self._last_login_attempt) < LOGIN_RETRY_COOLDOWN_SECONDS:
                return   # too soon since the last attempt -- skip this cycle, try again later
            self._last_login_attempt = time.time()
            self._login()

    def _looks_like_session_error(self, err_or_resp) -> bool:
        text = str(err_or_resp).lower()
        return any(hint in text for hint in self.SESSION_ERROR_HINTS)

    def _looks_like_rate_limit_error(self, err_or_resp) -> bool:
        text = str(err_or_resp).lower()
        return any(hint in text for hint in self.RATE_LIMIT_HINTS)

    def _call_with_relogin(self, fn, *args, **kwargs):
        """Resilience wrapper:
        - rate-limit-like error -> short backoff, retry same call (up to 2 extra tries)
        - session/token-like error -> relogin once, retry same call
        - anything else -> raise straight through, caller's try/except logs it
        """
        max_rate_limit_retries = 2
        for attempt in range(max_rate_limit_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if self._looks_like_rate_limit_error(e) and attempt < max_rate_limit_retries:
                    wait_s = 1.5 * (attempt + 1)
                    log.warning(f"Angel One rate-limit-like error ({e}) -- backing off {wait_s}s (attempt {attempt+1}/{max_rate_limit_retries}).")
                    time.sleep(wait_s)
                    continue
                if self._looks_like_session_error(e):
                    log.warning(f"Angel One call failed with a session-like error ({e}) -- relogging in and retrying once.")
                    self._login()
                    if self.client:
                        return fn(*args, **kwargs)
                raise

    def _load_instrument_master(self, force_refresh=False):
        """force_refresh=True (Milestone 17+: expiry_intelligence_cli.py's
        --live flag) skips the <24h cache-freshness check and always
        re-downloads -- for right after NSE/BSE/MCX publish a new expiry
        (new weekly series, holiday-shifted date) and the caller doesn't
        want to wait for the next scheduled refresh. Still writes the
        result back to the SAME cache file every normal cycle already
        uses (so the live dashboard's own next refresh sees the same
        fresh data, not a second diverging copy)."""
        try:
            if not force_refresh and os.path.exists(INSTRUMENT_MASTER_CACHE):
                mtime = os.path.getmtime(INSTRUMENT_MASTER_CACHE)
                if time.time() - mtime < 24 * 3600:
                    with open(INSTRUMENT_MASTER_CACHE, "r") as f:
                        self.instruments = json.load(f)
                    log.info(f"Loaded cached instrument master ({len(self.instruments)} rows).")
                    return
            r = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
            self.instruments = r.json()
            with open(INSTRUMENT_MASTER_CACHE, "w") as f:
                json.dump(self.instruments, f)
            log.info(f"Downloaded fresh instrument master ({len(self.instruments)} rows).")
        except Exception as e:
            log.error(f"Instrument master download failed: {e}")
            self.instruments = []

    # -- underlying price resolution ----------------------------------------

    def get_index_spot_ltp(self, symbol: str) -> float:
        """For index_option / index_spot symbols. Uses hardcoded token if known,
        else searches the instrument master (best-effort -- verify if it returns 0)."""
        cfg = SYMBOLS[symbol]
        token = cfg.get("spot_token")
        exch = cfg["exch"]
        if not token:
            token = self._spot_token_cache.get(symbol)
            if not token:
                target = symbol.replace(" ", "")
                candidates = [
                    row for row in self.instruments
                    if row.get("exch_seg") == exch
                    and (row.get("symbol", "").upper().replace("-EQ", "").replace(" ", "") == target
                         or row.get("name", "").upper().replace(" ", "") == target)
                    and row.get("instrumenttype", "") in ("", "AMXIDX")
                ]
                # Prefer AMXIDX matches first -- the correct, currently-active
                # index entry (e.g. MIDCPNIFTY: token 99926074, name="MIDCPNIFTY",
                # symbol="NIFTY MID SELECT") has instrumenttype AMXIDX, matching
                # every other working index (NIFTY, BANKNIFTY, etc). A stale/
                # non-functional duplicate entry with empty instrumenttype and
                # lotsize=-1 was being matched instead (token 26074), found
                # 2026-07-24 when historical-candle fetches for MIDCPNIFTY
                # returned genuinely empty data despite a "successful" API call.
                candidates.sort(key=lambda r: 0 if r.get("instrumenttype") == "AMXIDX" else 1)
                if candidates:
                    token = candidates[0].get("token")
                    self._spot_token_cache[symbol] = token
        self._ensure_session_fresh()   # retries login if self.client went None from a prior failure -- CRITICAL, else one failed login permanently breaks the app until manual restart
        if not self.client or not token:
            return 0.0
        self._ensure_session_fresh()
        try:
            resp = self._call_with_relogin(self.client.ltpData, exch, symbol, token)
            if resp and resp.get("status"):
                return float(resp["data"]["ltp"])
        except Exception as e:
            log.warning(f"Angel spot LTP fetch failed for {symbol}: {e}")
        return 0.0

    def get_commodity_underlying(self, symbol: str):
        """MCX commodities have no simple index -- use nearest-expiry futures (FUTCOM)
        contract's LTP as the underlying price proxy. Returns (ltp, future_token)."""
        cache_key = symbol
        token = self._future_token_cache.get(cache_key)
        if not token:
            candidates = [
                row for row in self.instruments
                if row.get("name") == symbol
                and row.get("instrumenttype") == "FUTCOM"
                and row.get("exch_seg") == "MCX"
            ]
            if not candidates:
                return 0.0, None
            today = now_ist().date()
            upcoming = [c for c in candidates if parse_expiry(c.get("expiry", "")) >= today]
            pool = upcoming or candidates   # fall back to all if somehow none are upcoming
            pool.sort(key=lambda r: parse_expiry(r.get("expiry", "")))
            token = pool[0].get("token")
            self._future_token_cache[cache_key] = token
        self._ensure_session_fresh()   # retries login if self.client went None -- same fix as spot LTP
        if not self.client or not token:
            return 0.0, token
        self._ensure_session_fresh()
        try:
            resp = self._call_with_relogin(self.client.ltpData, "MCX", symbol, token)
            if resp and resp.get("status"):
                return float(resp["data"]["ltp"]), token
        except Exception as e:
            log.warning(f"Angel commodity future LTP fetch failed for {symbol}: {e}")
        return 0.0, token

    # -- option token resolution ---------------------------------------------

    def find_option_token(self, symbol: str, strike: int, opt_type: str, cfg: dict):
        is_commodity = cfg["type"] in COMMODITY_TYPES
        wanted_instrumenttypes = ("OPTFUT",) if is_commodity else ("OPTIDX", "OPTSTK")
        exch_seg = "MCX" if is_commodity else cfg.get("options_exch_seg", "NFO")

        candidates = [
            row for row in self.instruments
            if row.get("name") == symbol
            and row.get("exch_seg") == exch_seg
            and row.get("instrumenttype") in wanted_instrumenttypes
            and row.get("symbol", "").endswith(opt_type)
            and str(row.get("strike", "")).replace(".000000", "") == str(strike * 100)
        ]
        if not candidates:
            # Fallback: extract the numeric strike run immediately preceding the
            # CE/PE suffix and require an EXACT match -- a plain substring check
            # (`f"{strike}" in symbol`) would let e.g. strike 500 match inside
            # "...25000CE" and silently resolve the wrong strike.
            strike_str = str(int(strike))
            candidates = []
            for row in self.instruments:
                if row.get("name") != symbol or row.get("exch_seg") != exch_seg:
                    continue
                sym = row.get("symbol", "")
                m = re.search(r"(\d+)" + re.escape(opt_type) + r"$", sym)
                if m and m.group(1) == strike_str:
                    candidates.append(row)
        if not candidates:
            return None, None
        today = now_ist().date()
        upcoming = [c for c in candidates if parse_expiry(c.get("expiry", "")) >= today]
        pool = upcoming or candidates
        pool.sort(key=lambda r: parse_expiry(r.get("expiry", "")))
        chosen = pool[0]
        return chosen.get("token"), chosen.get("symbol")

    def get_option_tokens_for_strikes(self, symbol: str, strikes: list, cfg: dict):
        out = {}
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                out[(strike, opt_type)] = self.find_option_token(symbol, strike, opt_type, cfg)
        return out

    def get_market_quotes(self, tokens: list, exchange: str) -> dict:
        self._ensure_session_fresh()   # retries login if self.client went None -- same critical fix
        if not self.client or not tokens:
            return {}
        self._ensure_session_fresh()
        result = {}
        for i in range(0, len(tokens), 50):
            chunk = tokens[i:i + 50]
            try:
                resp = self._call_with_relogin(self.client.getMarketData, mode="FULL", exchangeTokens={exchange: chunk})
                if not resp or not resp.get("status"):
                    if resp and self._looks_like_session_error(resp.get("message", "")):
                        log.warning(f"getMarketData returned a session-like error: {resp.get('message')}")
                    continue
                for item in resp.get("data", {}).get("fetched", []):
                    tok = item.get("symbolToken")
                    result[tok] = {
                        "ltp": float(item.get("ltp", 0) or 0),
                        "opnInterest": int(item.get("opnInterest", 0) or 0),
                        "tradeVolume": int(item.get("tradeVolume", 0) or 0),
                        "percentChange": float(item.get("percentChange", 0) or 0),
                    }
            except Exception as e:
                log.warning(f"getMarketData failed: {e}")
        return result

    def is_expiry_today(self, symbol: str, cfg: dict) -> bool:
        """Cached once per day per symbol -- avoids scanning the 160k-row
        instrument master every single cycle. True only when today's date
        exactly matches the nearest expiry actually being traded for this
        symbol's options."""
        cache = getattr(self, "_expiry_day_cache", None)
        if cache is None:
            cache = self._expiry_day_cache = {}
        today = now_ist().date()
        cached = cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]

        is_commodity = cfg["type"] in COMMODITY_TYPES
        wanted_instrumenttypes = ("OPTFUT",) if is_commodity else ("OPTIDX", "OPTSTK")
        exch_seg = "MCX" if is_commodity else cfg.get("options_exch_seg", "NFO")
        candidates = [
            row for row in self.instruments
            if row.get("name") == symbol and row.get("exch_seg") == exch_seg
            and row.get("instrumenttype") in wanted_instrumenttypes
        ]
        result = False
        if candidates:
            upcoming = [c for c in candidates if parse_expiry(c.get("expiry", "")) >= today]
            pool = upcoming or candidates
            pool.sort(key=lambda r: parse_expiry(r.get("expiry", "")))
            nearest_expiry = parse_expiry(pool[0].get("expiry", ""))
            result = (nearest_expiry == today)
        cache[symbol] = (today, result)
        return result

    def get_underlying_token_for_candles(self, symbol: str, cfg: dict):
        """Reuses tokens already resolved/cached by get_index_spot_ltp / get_commodity_underlying
        (which must have run at least once this session -- true by the time this is called
        in run_symbol_loop, since it's called after the LTP fetch)."""
        if cfg["type"] in COMMODITY_TYPES:
            return self._future_token_cache.get(symbol), "MCX"
        token = cfg.get("spot_token") or self._spot_token_cache.get(symbol)
        return token, cfg["exch"]

    def list_available_expiries(self, symbol: str):
        """
        Every distinct expiry date Angel One's OWN cached instrument master
        currently lists for this symbol's options/futures, sorted chronologically
        ascending. Extracted out of find_nearest_expiry() below (Milestone 17+)
        so expiry_intelligence.py can get the FULL calendar -- not just the
        nearest date -- without re-scanning self.instruments itself (there is
        exactly one place this 160k-row scan should happen).

        Deliberately independent of NSE-data (nse_data["expiry"]) -- MCX
        commodity symbols (NATURALGAS, CRUDEOIL, GOLD, etc.) never have NSE
        option-chain data ("NSE=N/A for this instrument" in logs), so relying
        on NSE's expiry-string would silently skip this for every commodity
        symbol. This uses Angel One's own instrument master instead, which
        covers ALL exchanges (NFO, MCX, BFO).

        Returns [] (never None) when the instrument master isn't loaded yet
        or the symbol has no matching rows -- callers can treat "no expiries"
        uniformly without a None-check.
        """
        if not hasattr(self, "instruments") or not self.instruments:
            return []
        candidates = [
            row for row in self.instruments
            if row.get("name") == symbol and row.get("instrumenttype") in ("OPTIDX", "OPTSTK", "OPTFUT")
            and row.get("expiry")
        ]
        if not candidates:
            return []
        unique_expiries = {row["expiry"] for row in candidates}
        parsed = []
        for exp_str in unique_expiries:
            try:
                parsed.append(dt.datetime.strptime(exp_str, "%d%b%Y").date())
            except ValueError:
                continue   # skip any malformed expiry-string rather than crashing
        parsed.sort()   # genuine chronological order, not alphabetical string-sort
        return parsed

    def find_nearest_expiry(self, symbol: str):
        """
        Nearest upcoming expiry for `symbol`, formatted as DDMMMYYYY (e.g.
        '01SEP2026') -- the exact format optionGreek() requires. Thin
        formatting wrapper around list_available_expiries() (see its
        docstring for the data-source rationale).
        """
        dates = self.list_available_expiries(symbol)
        if not dates:
            return None
        return dates[0].strftime("%d%b%Y").upper()

    def get_option_greeks(self, symbol: str, expiry_ddmmmyyyy: str) -> dict:
        """
        Fetches live Delta/Gamma/Theta/Vega/IV for ALL strikes of a symbol's
        given expiry, via Angel One's optionGreek endpoint (confirmed-working
        via manual exploration -- see explore_angelone_data.py).

        expiry_ddmmmyyyy: exact format Angel One requires, e.g. "01SEP2026"
        (NOT the same format used elsewhere in this codebase -- confirmed
        via Angel One's own SmartAPI forum documentation).

        Returns {(strike: float, optiontype: "CE"|"PE"): {"delta":.., "gamma":..,
        "theta":.., "vega":.., "iv":..}} for O(1) lookup per-strike, or {} on
        any failure (never raises -- Greeks are informational/supplementary,
        never allowed to break the core live-data loop if unavailable).
        """
        self._ensure_session_fresh()
        if not self.client or not expiry_ddmmmyyyy:
            return {}
        try:
            resp = self._call_with_relogin(self.client.optionGreek, {"name": symbol, "expirydate": expiry_ddmmmyyyy})
            if not resp or not resp.get("status") or not resp.get("data"):
                return {}
            result = {}
            for row in resp["data"]:
                try:
                    strike = float(row["strikePrice"])
                    opt_type = row["optionType"]
                    result[(strike, opt_type)] = {
                        "delta": float(row["delta"]), "gamma": float(row["gamma"]),
                        "theta": float(row["theta"]), "vega": float(row["vega"]),
                        "iv": float(row["impliedVolatility"]),
                    }
                except (KeyError, ValueError, TypeError):
                    continue   # skip any malformed row rather than failing the whole batch
            return result
        except Exception as e:
            log.warning(f"get_option_greeks failed for {symbol}: {e}")
            return {}

    def get_open_positions(self):
        """
        Fetches CURRENT open positions from Angel One (manually-placed trades
        included) via SmartAPI's position() endpoint. Read-only -- does not
        place, modify, or close any order.

        Returns a list of {symbol, token, exchange, product, buy_avg_price,
        net_qty, ltp, pnl} dicts for positions with non-zero net quantity
        (i.e. genuinely still open), or [] on any failure/no-session.
        """
        self._ensure_session_fresh()
        if not self.client:
            return []
        try:
            resp = self._call_with_relogin(self.client.position)
            if not resp or not resp.get("status") or not resp.get("data"):
                return []
            positions = []
            for p in resp["data"]:
                try:
                    net_qty = int(p.get("netqty", 0))
                except (ValueError, TypeError):
                    net_qty = 0
                if net_qty == 0:
                    continue   # closed/flat position -- nothing to monitor
                positions.append({
                    "symbol": p.get("tradingsymbol"), "token": p.get("symboltoken"),
                    "exchange": p.get("exchange"), "product": p.get("producttype"),
                    "buy_avg_price": float(p.get("avgnetprice") or 0),
                    "net_qty": net_qty, "ltp": float(p.get("ltp") or 0),
                    "pnl": float(p.get("pnl") or 0),
                })
            return positions
        except Exception as e:
            log.warning(f"get_open_positions failed: {e}")
            return []

    def get_historical_candles_range(self, token: str, exchange: str, interval: str,
                                      from_dt: "dt.datetime", to_dt: "dt.datetime") -> list:
        """
        Same as get_historical_candles, but for an EXPLICIT date range
        instead of 'N days back from now' -- used by history_engine.py for
        chunked historical downloads (e.g. fetching a year of data in
        30-day chunks with specific from/to boundaries).
        """
        # NOTE: unlike get_historical_candles below, this RAISES on genuine
        # failure (bad session, API error, exception) instead of swallowing
        # it into an empty list -- history_engine.py's retry logic needs to
        # tell "no candles in range" (holiday/no trading day, a real []
        # result) apart from "the fetch itself failed" (a raised exception),
        # otherwise a persistent outage looks identical to "already up to
        # date" and gets silently reported as such.
        self._ensure_session_fresh()
        if not self.client or not token:
            raise RuntimeError("Angel One session/client not available")
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self._call_with_relogin(self.client.getCandleData, params)
        if not resp or not resp.get("status"):
            raise RuntimeError(f"getCandleData (range) failed for token {token}: {resp}")
        candles = []
        for row in resp.get("data") or []:
            ts = dt.datetime.fromisoformat(row[0])
            candles.append({
                "datetime": ts.replace(tzinfo=None),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": int(row[5]) if len(row) > 5 else 0,
            })
        return candles

    def get_historical_candles(self, token: str, exchange: str, interval: str = "THREE_MINUTE", days: int = 3) -> list:
        """Real broker OHLCV via Angel One getCandleData -- foundation for ATR/
        swing-levels/PDH-PDL/VWAP/pivots (see market_structure.py). Returns a
        list of {'datetime', 'open', 'high', 'low', 'close', 'volume'}, oldest first.
        interval options (Angel-supported): ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE,
        TEN_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY."""
        self._ensure_session_fresh()   # retries login if self.client went None -- same critical fix
        if not self.client or not token:
            return []
        self._ensure_session_fresh()
        try:
            to_dt = dt.datetime.now()
            from_dt = to_dt - dt.timedelta(days=days)
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self._call_with_relogin(self.client.getCandleData, params)
            if not resp or not resp.get("status") or not resp.get("data"):
                return []
            candles = []
            for row in resp["data"]:
                # Angel format: [timestamp_iso, open, high, low, close, volume]
                ts = dt.datetime.fromisoformat(row[0])
                candles.append({
                    "datetime": ts.replace(tzinfo=None),
                    "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]),
                    "volume": int(row[5]) if len(row) > 5 else 0,
                })
            return candles
        except Exception as e:
            log.warning(f"getCandleData failed for token {token}: {e}")
            return []


# ----------------------------------------------------------------------------
# OI / VOLUME ANALYZER (institutional mathematics)
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Shared OI analysis + signal engine (imported so live + backtest use IDENTICAL logic)
# ----------------------------------------------------------------------------

from oi_engine import (
    StrikeRow, classify_buildup, find_atm, wanted_strikes,
    calc_pcr, calc_max_pain, oi_walls, detect_bias, generate_signal as _generate_signal_raw,
    compute_trend_meter, compute_conviction_strength, compute_new_trend_meter,
)
from ichimoku_engine import analyze as ichimoku_analyze

ICHIMOKU_PAPER_MAX_HOLD_MINUTES = 60   # mirrors backtest.py's ICHIMOKU_MAX_HOLD_MINUTES -- kept as a local
                                        # constant rather than importing backtest.py at module level into app.py


def generate_signal(rows, atm, bias, note, pcr, support, resistance,
                     nse_atm_row=None, underlying=None, expiry_date=None, strike_step=50, source_label="NSE",
                     market_structure=None):
    """Thin wrapper binding this app's configured thresholds to the shared engine."""
    return _generate_signal_raw(
        rows, atm, bias, note, pcr, support, resistance,
        target_delta_approx=TARGET_DELTA_APPROX, sl_percent=SL_PERCENT,
        min_target_percent=MIN_TARGET_PERCENT, confidence_threshold=state["dev_settings"]["SIGNAL_CONFIDENCE_THRESHOLD"],
        nse_atm_row=nse_atm_row, underlying=underlying, expiry_date=expiry_date, strike_step=strike_step,
        source_label=source_label, market_structure=market_structure,
        structural_proximity_atr_mult=STRUCTURAL_PROXIMITY_ATR_MULT,
        structural_bonus=STRUCTURAL_BONUS, structural_penalty=STRUCTURAL_PENALTY,
    )


def build_strike_rows(angel: AngelOneFetcher, symbol: str, underlying: float, step: int,
                       cfg: dict, prev_state: dict):
    wanted, atm = wanted_strikes(underlying, step, STRIKES_EACH_SIDE)
    token_map = angel.get_option_tokens_for_strikes(symbol, wanted, cfg)
    all_tokens = [tok for tok, _ in token_map.values() if tok]
    exch = "MCX" if cfg["type"] in COMMODITY_TYPES else cfg.get("options_exch_seg", "NFO")
    quotes = angel.get_market_quotes(all_tokens, exch)

    is_expiry_day = angel.is_expiry_today(symbol, cfg)
    min_price_chg_pct = EXPIRY_DAY_MIN_PRICE_CHG_PCT if is_expiry_day else 0

    rows = {s: StrikeRow(strike=s) for s in wanted}
    for (strike, opt_type), (token, _tsym) in token_map.items():
        if not token or token not in quotes:
            continue
        q = quotes[token]
        prev = prev_state.get(token, {})
        oi_chg = q["opnInterest"] - prev.get("oi", q["opnInterest"])
        chg_pct = q["percentChange"] if q["percentChange"] else (
            round((q["ltp"] - prev.get("ltp", q["ltp"])) / prev["ltp"] * 100, 2) if prev.get("ltp") else 0.0
        )
        signal = classify_buildup(
            chg_pct, oi_chg, MIN_OI_CHANGE_THRESHOLD,
            prev_oi=prev.get("oi"), min_oi_chg_pct=MIN_OI_CHANGE_PERCENT,
            min_price_chg_pct=min_price_chg_pct,
        ) if token in prev_state else "Neutral"

        row = rows[strike]
        if opt_type == "CE":
            row.ce_oi, row.ce_oi_chg, row.ce_vol = q["opnInterest"], oi_chg, q["tradeVolume"]
            row.ce_ltp, row.ce_chg_pct, row.ce_signal = q["ltp"], chg_pct, signal
        else:
            row.pe_oi, row.pe_oi_chg, row.pe_vol = q["opnInterest"], oi_chg, q["tradeVolume"]
            row.pe_ltp, row.pe_chg_pct, row.pe_signal = q["ltp"], chg_pct, signal
        prev_state[token] = {"oi": q["opnInterest"], "ltp": q["ltp"]}

    return [rows[s] for s in wanted], atm


# ----------------------------------------------------------------------------
# PAPER TRADING ENGINE -- virtual money only, never places a real order.
# Auto-opens a paper position when a tradeable signal fires (and none is open),
# auto-closes on target / stop-loss / max hold time.
# ----------------------------------------------------------------------------

def paper_trade_bucket(symbol):
    d = state["paper_by_symbol"]
    if symbol not in d:
        d[symbol] = {
            "open_trade": None,
            "history": deque(maxlen=100),
            "wins": 0, "losses": 0, "time_exits": 0,
            "total_points": 0.0,
        }
    return d[symbol]


def save_market_structure_snapshot(symbol, structure):
    """
    Persists a snapshot of what the live app GENUINELY computed for market
    structure (ATR, regime, PDH/PDL, mother candle, liquidity sweep) --
    exactly once per day per symbol, right when it's freshly computed. Future
    backtests can join against this real data instead of using the pseudo-
    ATR/OI-wall approximation, with zero lookahead-bias risk since this is
    precisely what was known live at that moment (not reconstructed later
    with hindsight).
    """
    try:
        now = now_ist()
        pdl = structure.get("prev_day") or {}
        mother = structure.get("mother_candle") or {}
        sweep = structure.get("liquidity_sweep") or {}
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO market_structure_snapshots
               (symbol, date, time, ts, atr_14, adx, regime, pdh, pdl, pdc, vwap,
                swing_high, swing_low, mother_candle_json, liquidity_sweep_json, custom_levels_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), now.isoformat(),
             structure.get("atr_14"), structure.get("adx"), structure.get("regime"),
             pdl.get("pdh"), pdl.get("pdl"), pdl.get("pdc"), structure.get("vwap"),
             structure.get("swing_high"), structure.get("swing_low"),
             json.dumps(mother), json.dumps(sweep), json.dumps(structure.get("custom_levels")),
             ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"Failed to save market-structure snapshot for {symbol}: {e}")


def db_open_paper_trade(symbol, trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO paper_trades (symbol, strike, direction, entry_price, target_price, sl_price,
               confidence, entry_time, entry_ts, status, institutional_score, institutional_tier,
               regime_at_entry, sr_level, risk_reward) VALUES (?,?,?,?,?,?,?,?,?, 'OPEN', ?,?,?,?,?)""",
            (symbol, trade["strike"], trade["direction"], trade["entry_price"], trade["target_price"],
             trade["sl_price"], trade["confidence"], trade["entry_time"], trade["entry_time_obj"].timestamp(),
             trade.get("institutional_score"), trade.get("institutional_tier"),
             trade.get("regime_at_entry"), trade.get("sr_level"), trade.get("risk_reward")),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        log.warning(f"DB open paper trade failed: {e}")
        return None


def db_close_paper_trade(db_id, exit_price, exit_time, exit_reason, points):
    if db_id is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """UPDATE paper_trades SET exit_price=?, exit_time=?, exit_reason=?, points=?, status='CLOSED'
               WHERE id=?""",
            (exit_price, exit_time, exit_reason, points, db_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"DB close paper trade failed: {e}")


def load_paper_state_from_db():
    """Called once at startup -- restores open positions and trade history/stats
    for every symbol that has paper-trade rows, so a restart doesn't wipe progress."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        symbols = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM paper_trades").fetchall()]
        for symbol in symbols:
            bucket = paper_trade_bucket(symbol)

            open_row = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,)
            ).fetchone()
            if open_row:
                bucket["open_trade"] = {
                    "symbol": symbol, "strike": open_row["strike"], "direction": open_row["direction"],
                    "entry_price": open_row["entry_price"], "target_price": open_row["target_price"],
                    "sl_price": open_row["sl_price"], "entry_time": open_row["entry_time"],
                    "entry_time_obj": dt.datetime.fromtimestamp(open_row["entry_ts"]),
                    "confidence": open_row["confidence"], "current_price": open_row["entry_price"],
                    "points_now": 0.0, "db_id": open_row["id"],
                }

            closed_rows = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol=? AND status='CLOSED' ORDER BY id DESC LIMIT 200",
                (symbol,),
            ).fetchall()
            today = now_ist().date()
            todays_rows = [
                r for r in closed_rows
                if r["entry_ts"] and dt.datetime.fromtimestamp(r["entry_ts"]).date() == today
            ]
            for r in reversed(todays_rows):   # oldest-first, so history displays newest-first via appendleft-equivalent order
                trade = {
                    "symbol": symbol, "strike": r["strike"], "direction": r["direction"],
                    "entry_price": r["entry_price"], "target_price": r["target_price"], "sl_price": r["sl_price"],
                    "entry_time": r["entry_time"], "confidence": r["confidence"],
                    "exit_price": r["exit_price"], "exit_time": r["exit_time"],
                    "exit_reason": r["exit_reason"], "points": r["points"],
                }
                bucket["history"].appendleft(trade)
                bucket["total_points"] += r["points"] or 0
                if r["exit_reason"] == "TARGET HIT":
                    bucket["wins"] += 1
                elif r["exit_reason"] == "STOP LOSS":
                    bucket["losses"] += 1
                else:
                    bucket["time_exits"] += 1

            log.info(f"Restored paper-trade state for {symbol}: {len(todays_rows)} closed trades TODAY "
                      f"(of {len(closed_rows)} total in DB, older ones excluded from stats), "
                      f"open={'yes' if open_row else 'no'}")
        conn.close()
    except Exception as e:
        log.warning(f"Loading paper state from DB failed: {e}")


def scalp_paper_trade_bucket(symbol):
    d = state["scalp_paper_by_symbol"]
    if symbol not in d:
        d[symbol] = {
            "open_trade": None,
            "history": deque(maxlen=100),
            "wins": 0, "losses": 0, "time_exits": 0,
            "total_points": 0.0,
        }
    return d[symbol]


def db_open_scalp_paper_trade(symbol, trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO scalp_paper_trades (symbol, strike, direction, entry_price, target_price, sl_price,
               entry_time, entry_ts, status, risk_reward, delta_used, regime_multiplier, volume_ratio)
               VALUES (?,?,?,?,?,?,?,?, 'OPEN', ?,?,?,?)""",
            (symbol, trade["strike"], trade["direction"], trade["entry_price"], trade["target_price"],
             trade["sl_price"], trade["entry_time"], trade["entry_time_obj"].timestamp(),
             trade.get("risk_reward"), trade.get("delta_used"), trade.get("regime_multiplier"), trade.get("volume_ratio")),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        log.warning(f"DB open scalp paper trade failed: {e}")
        return None


def db_close_scalp_paper_trade(db_id, exit_price, exit_time, exit_reason, points):
    if db_id is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """UPDATE scalp_paper_trades SET exit_price=?, exit_time=?, exit_reason=?, points=?, status='CLOSED'
               WHERE id=?""",
            (exit_price, exit_time, exit_reason, points, db_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"DB close scalp paper trade failed: {e}")


def load_scalp_paper_state_from_db():
    """Called once at startup -- restores open scalp positions and today's
    trade history/stats, same pattern as load_paper_state_from_db() but
    against the separate scalp_paper_trades table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        symbols = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM scalp_paper_trades").fetchall()]
        for symbol in symbols:
            bucket = scalp_paper_trade_bucket(symbol)

            open_row = conn.execute(
                "SELECT * FROM scalp_paper_trades WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,)
            ).fetchone()
            if open_row:
                bucket["open_trade"] = {
                    "symbol": symbol, "strike": open_row["strike"], "direction": open_row["direction"],
                    "entry_price": open_row["entry_price"], "target_price": open_row["target_price"],
                    "sl_price": open_row["sl_price"], "entry_time": open_row["entry_time"],
                    "entry_time_obj": dt.datetime.fromtimestamp(open_row["entry_ts"]),
                    "current_price": open_row["entry_price"], "points_now": 0.0, "db_id": open_row["id"],
                    "risk_reward": open_row["risk_reward"], "delta_used": open_row["delta_used"],
                    "regime_multiplier": open_row["regime_multiplier"], "volume_ratio": open_row["volume_ratio"],
                }

            closed_rows = conn.execute(
                "SELECT * FROM scalp_paper_trades WHERE symbol=? AND status='CLOSED' ORDER BY id DESC LIMIT 200",
                (symbol,),
            ).fetchall()
            today = now_ist().date()
            todays_rows = [
                r for r in closed_rows
                if r["entry_ts"] and dt.datetime.fromtimestamp(r["entry_ts"]).date() == today
            ]
            for r in reversed(todays_rows):
                trade = {
                    "symbol": symbol, "strike": r["strike"], "direction": r["direction"],
                    "entry_price": r["entry_price"], "target_price": r["target_price"], "sl_price": r["sl_price"],
                    "entry_time": r["entry_time"], "exit_price": r["exit_price"], "exit_time": r["exit_time"],
                    "exit_reason": r["exit_reason"], "points": r["points"], "risk_reward": r["risk_reward"],
                }
                bucket["history"].appendleft(trade)
                bucket["total_points"] += r["points"] or 0
                if r["exit_reason"] == "TARGET HIT":
                    bucket["wins"] += 1
                elif r["exit_reason"] == "STOP LOSS":
                    bucket["losses"] += 1
                else:
                    bucket["time_exits"] += 1

            log.info(f"Restored scalp paper-trade state for {symbol}: {len(todays_rows)} closed trades TODAY "
                      f"(of {len(closed_rows)} total in DB), open={'yes' if open_row else 'no'}")
        conn.close()
    except Exception as e:
        log.warning(f"Loading scalp paper state from DB failed: {e}")


def select_best_scalp_candidate(scalp_signal):
    """Picks the higher-conviction tradeable candidate out of a scalp_signal
    dict (keyed 'CE'/'PE') -- both sides qualifying in the same cycle is
    unusual but possible (see scalping_engine.generate_scalp_signal). Shared
    by the Scalp engine's own system-wide reference-trade tracking
    (update_scalp_paper_trading, below) and the per-user AI Auto-Trading
    fan-out (fanout_auto_trade_entry) -- extracted so both always agree on
    which candidate is "the" signal for a given cycle, rather than risking
    two independent copies of this selection logic drifting apart. Returns
    the candidate dict, or None if nothing tradeable this cycle."""
    if not scalp_signal:
        return None
    candidates = [s for s in scalp_signal.values() if s and s.get("tradeable")]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("risk_reward") or 0)


def update_scalp_paper_trading(symbol, scalp_signal, rows, now_str):
    """
    Trade lifecycle for the Scalping Engine's OWN, separate paper-trading
    bucket -- never mixed with the S/R engine's update_paper_trading() above.
    Deliberately simpler than that function: fixed target/SL from entry (no
    trailing-SL staging -- scalping.py's target/SL are already tight and the
    hold is only a few minutes, so there's little room for a trailing stage
    to matter), and a much shorter time-exit (SCALP_MAX_HOLD_MINUTES vs the
    swing engine's MAX_HOLD_MINUTES).
    """
    if not state["dev_settings"]["PAPER_TRADING_ENABLED"]:
        return None
    bucket = scalp_paper_trade_bucket(symbol)
    open_trade = bucket["open_trade"]

    if open_trade:
        strike, direction = open_trade["strike"], open_trade["direction"]
        row = next((r for r in rows if r.strike == strike), None)
        current_price = (row.ce_ltp if direction == "CE" else row.pe_ltp) if row else None
        if current_price:
            open_trade["current_price"] = current_price
            open_trade["points_now"] = round(current_price - open_trade["entry_price"], 2)

            held_minutes = (now_ist() - open_trade["entry_time_obj"]).total_seconds() / 60
            exit_reason = None
            if current_price >= open_trade["target_price"]:
                exit_reason = "TARGET HIT"
            elif current_price <= open_trade["sl_price"]:
                exit_reason = "STOP LOSS"
            elif held_minutes >= SCALP_MAX_HOLD_MINUTES:
                exit_reason = "TIME EXIT"

            if exit_reason:
                points = round(current_price - open_trade["entry_price"], 2)
                open_trade.update({"exit_price": current_price, "exit_time": now_str,
                                    "exit_reason": exit_reason, "points": points})
                db_close_scalp_paper_trade(open_trade.get("db_id"), current_price, now_str, exit_reason, points)
                bucket["history"].appendleft(open_trade)
                bucket["total_points"] += points
                if exit_reason == "TARGET HIT":
                    bucket["wins"] += 1
                elif exit_reason == "STOP LOSS":
                    bucket["losses"] += 1
                    state["scalp_cooldown_until_by_symbol"][symbol] = now_ist() + dt.timedelta(minutes=SCALP_COOLDOWN_MINUTES_AFTER_SL)
                else:
                    bucket["time_exits"] += 1
                bucket["open_trade"] = None
                close_msg = f"[{now_str}] SCALP PAPER TRADE CLOSED ({symbol} {strike}{direction}): {exit_reason}, {points:+.2f} pts"
                socketio.emit("alert", {"message": close_msg}, room=symbol)

    else:
        cooldown_until = state["scalp_cooldown_until_by_symbol"].get(symbol)
        in_cooldown = cooldown_until and now_ist() < cooldown_until
        if not in_cooldown and scalp_signal:
            best = select_best_scalp_candidate(scalp_signal)
            if best:
                new_trade = {
                    "symbol": symbol, "strike": best["strike"], "direction": best["direction"],
                    "entry_price": best["entry_price"], "target_price": best["target_price"],
                    "sl_price": best["sl_price"], "entry_time": now_str, "entry_time_obj": now_ist(),
                    "current_price": best["entry_price"], "points_now": 0.0,
                    "risk_reward": best.get("risk_reward"), "delta_used": best.get("delta_used"),
                    "regime_multiplier": best.get("regime_multiplier"), "volume_ratio": best.get("volume_ratio"),
                }
                new_trade["db_id"] = db_open_scalp_paper_trade(symbol, new_trade)
                bucket["open_trade"] = new_trade
                open_msg = (f"[{now_str}] SCALP PAPER TRADE OPENED ({symbol} {best['strike']}{best['direction']}) "
                            f"@ {best['entry_price']} | target {best['target_price']} | SL {best['sl_price']} | R:R {best.get('risk_reward')}")
                socketio.emit("alert", {"message": open_msg}, room=symbol)

    total_trades = bucket["wins"] + bucket["losses"] + bucket["time_exits"]
    win_rate = round(bucket["wins"] / total_trades * 100, 1) if total_trades else 0.0
    open_trade_out = None
    if bucket["open_trade"]:
        ot = bucket["open_trade"]
        open_trade_out = {k: v for k, v in ot.items() if k != "entry_time_obj"}
    return {
        "open_trade": open_trade_out,
        "history": [{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]],
        "wins": bucket["wins"], "losses": bucket["losses"], "time_exits": bucket["time_exits"],
        "win_rate": win_rate, "total_points": round(bucket["total_points"], 2),
    }


def ichimoku_paper_trade_bucket(symbol):
    d = state["ichimoku_paper_by_symbol"]
    if symbol not in d:
        d[symbol] = {"open_trade": None, "history": deque(maxlen=200), "wins": 0, "losses": 0, "time_exits": 0, "total_points": 0.0}
    return d[symbol]


def update_ichimoku_paper_trading(symbol, ichimoku_signal, underlying, now_str, max_hold_minutes=ICHIMOKU_PAPER_MAX_HOLD_MINUTES):
    """
    Ichimoku Engine's OWN, separate paper-trading track record -- logs every
    recommendation together with its ACTUAL market outcome (underlying
    points, not option premium -- see ichimoku_engine.py's module docstring)
    so win rate / precision / false-buy% / false-sell% / avg R:R can be
    measured (via backtest.compute_ichimoku_accuracy_stats, same shape:
    'direction'/'points'/'exit_reason'/'entry_time'/'exit_time') BEFORE this
    engine is ever trusted to influence a real order.

    ADVISORY ONLY: purely a bookkeeping simulation against the live LTP --
    never opens a real order, never calls fanout_auto_trade_entry.

    SCOPE NOTE: in-memory only (unlike the Scalp/V3 paper-trading buckets,
    there is no DB persistence table for this yet -- history resets on app
    restart). Added once this engine's live track record is being watched
    for longer than a process lifetime; not built speculatively here.
    """
    if not state["dev_settings"]["PAPER_TRADING_ENABLED"] or not ichimoku_signal:
        return None
    bucket = ichimoku_paper_trade_bucket(symbol)
    open_trade = bucket["open_trade"]

    if open_trade:
        direction = open_trade["direction"]
        hit_target = (direction == "BUY" and underlying >= open_trade["target_price"]) or \
                     (direction == "SELL" and underlying <= open_trade["target_price"])
        hit_stop = (direction == "BUY" and underlying <= open_trade["sl_price"]) or \
                   (direction == "SELL" and underlying >= open_trade["sl_price"])
        held_minutes = (now_ist() - open_trade["entry_time_obj"]).total_seconds() / 60
        exit_reason = "TARGET HIT" if hit_target else "STOP LOSS" if hit_stop else "TIME EXIT" if held_minutes >= max_hold_minutes else None
        if exit_reason:
            exit_price = open_trade["target_price"] if exit_reason == "TARGET HIT" else open_trade["sl_price"] if exit_reason == "STOP LOSS" else underlying
            points = round((exit_price - open_trade["entry_price"]) if direction == "BUY" else (open_trade["entry_price"] - exit_price), 2)
            closed = {**open_trade, "exit_price": exit_price, "exit_time": now_str, "exit_time_obj": now_ist(),
                      "exit_reason": exit_reason, "points": points}
            bucket["history"].appendleft(closed)
            bucket["total_points"] += points
            if exit_reason == "TARGET HIT":
                bucket["wins"] += 1
            elif exit_reason == "STOP LOSS":
                bucket["losses"] += 1
            else:
                bucket["time_exits"] += 1
            bucket["open_trade"] = None
            socketio.emit("alert", {"message": f"[{now_str}] ICHIMOKU PAPER TRADE CLOSED ({symbol} {direction}): {exit_reason}, {points:+.2f} pts"}, room=symbol)
    else:
        action = ichimoku_signal.get("entry_signal")
        risk = ichimoku_signal.get("risk_management") or {}
        if action in ("BUY", "STRONG BUY", "SELL", "STRONG SELL") and risk.get("initial_stop") is not None and risk.get("targets"):
            direction = "BUY" if action in ("BUY", "STRONG BUY") else "SELL"
            bucket["open_trade"] = {
                "symbol": symbol, "direction": direction, "entry_price": risk["entry"],
                "target_price": risk["targets"][0], "sl_price": risk["initial_stop"],
                "entry_time": now_str, "entry_time_obj": now_ist(),
                "confidence": ichimoku_signal.get("confidence_score"), "trend_stage": ichimoku_signal.get("trend_stage"),
                "entry_signal": action, "risk_reward": risk.get("risk_reward"),
            }
            socketio.emit("alert", {"message": f"[{now_str}] ICHIMOKU PAPER TRADE OPENED ({symbol} {direction}) "
                                                f"@ {risk['entry']} | target {risk['targets'][0]} | SL {risk['initial_stop']}"}, room=symbol)

    total_trades = bucket["wins"] + bucket["losses"] + bucket["time_exits"]
    win_rate = round(bucket["wins"] / total_trades * 100, 1) if total_trades else 0.0
    open_trade_out = {k: v for k, v in bucket["open_trade"].items() if k != "entry_time_obj"} if bucket["open_trade"] else None
    return {
        "open_trade": open_trade_out,
        "history": [{k: v for k, v in t.items() if k not in ("entry_time_obj", "exit_time_obj")} for t in list(bucket["history"])[:15]],
        "wins": bucket["wins"], "losses": bucket["losses"], "time_exits": bucket["time_exits"],
        "win_rate": win_rate, "total_points": round(bucket["total_points"], 2),
    }


# ----------------------------------------------------------------------------
# S/R ENGINE V3 -- own separate paper-trading track record (see sr_engine_v3.py
# for the scoring/detection logic itself). OFF by default per-symbol; mirrors
# the Scalping Engine's paper-trading wiring exactly (scalp_paper_trade_bucket
# family, above), just against the v3_paper_trades table and generate_v3_signal's
# single trade_decision/direction shape (no "select best of CE/PE" needed --
# generate_v3_signal already picks the higher-confidence side itself).
# ----------------------------------------------------------------------------

def v3_paper_trade_bucket(symbol):
    d = state["v3_paper_by_symbol"]
    if symbol not in d:
        d[symbol] = {
            "open_trade": None,
            "history": deque(maxlen=100),
            "wins": 0, "losses": 0, "time_exits": 0,
            "total_points": 0.0,
        }
    return d[symbol]


def db_open_v3_paper_trade(symbol, trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO v3_paper_trades (symbol, strike, direction, entry_price, target_price, sl_price,
               entry_time, entry_ts, status, risk_reward, confidence, regime_at_entry, prev_day_validation,
               factors_json)
               VALUES (?,?,?,?,?,?,?,?, 'OPEN', ?,?,?,?,?)""",
            (symbol, trade["strike"], trade["direction"], trade["entry_price"], trade["target_price"],
             trade["sl_price"], trade["entry_time"], trade["entry_time_obj"].timestamp(),
             trade.get("risk_reward"), trade.get("confidence"), trade.get("regime_at_entry"),
             trade.get("prev_day_validation"), trade.get("factors_json")),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        log.warning(f"DB open v3 paper trade failed: {e}")
        return None


def db_close_v3_paper_trade(db_id, exit_price, exit_time, exit_reason, points):
    if db_id is None:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """UPDATE v3_paper_trades SET exit_price=?, exit_time=?, exit_reason=?, points=?, status='CLOSED'
               WHERE id=?""",
            (exit_price, exit_time, exit_reason, points, db_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"DB close v3 paper trade failed: {e}")


def load_v3_paper_state_from_db():
    """Called once at startup -- restores open V3 positions and today's trade
    history/stats, same pattern as load_scalp_paper_state_from_db() but
    against the separate v3_paper_trades table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        symbols = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM v3_paper_trades").fetchall()]
        for symbol in symbols:
            bucket = v3_paper_trade_bucket(symbol)

            open_row = conn.execute(
                "SELECT * FROM v3_paper_trades WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,)
            ).fetchone()
            if open_row:
                bucket["open_trade"] = {
                    "symbol": symbol, "strike": open_row["strike"], "direction": open_row["direction"],
                    "entry_price": open_row["entry_price"], "target_price": open_row["target_price"],
                    "sl_price": open_row["sl_price"], "entry_time": open_row["entry_time"],
                    "entry_time_obj": dt.datetime.fromtimestamp(open_row["entry_ts"]),
                    "current_price": open_row["entry_price"], "points_now": 0.0, "db_id": open_row["id"],
                    "risk_reward": open_row["risk_reward"], "confidence": open_row["confidence"],
                    "regime_at_entry": open_row["regime_at_entry"],
                }

            closed_rows = conn.execute(
                "SELECT * FROM v3_paper_trades WHERE symbol=? AND status='CLOSED' ORDER BY id DESC LIMIT 200",
                (symbol,),
            ).fetchall()
            today = now_ist().date()
            todays_rows = [
                r for r in closed_rows
                if r["entry_ts"] and dt.datetime.fromtimestamp(r["entry_ts"]).date() == today
            ]
            for r in reversed(todays_rows):
                trade = {
                    "symbol": symbol, "strike": r["strike"], "direction": r["direction"],
                    "entry_price": r["entry_price"], "target_price": r["target_price"], "sl_price": r["sl_price"],
                    "entry_time": r["entry_time"], "exit_price": r["exit_price"], "exit_time": r["exit_time"],
                    "exit_reason": r["exit_reason"], "points": r["points"], "risk_reward": r["risk_reward"],
                }
                bucket["history"].appendleft(trade)
                bucket["total_points"] += r["points"] or 0
                if r["exit_reason"] == "TARGET HIT":
                    bucket["wins"] += 1
                elif r["exit_reason"] == "STOP LOSS":
                    bucket["losses"] += 1
                else:
                    bucket["time_exits"] += 1

            log.info(f"Restored V3 paper-trade state for {symbol}: {len(todays_rows)} closed trades TODAY "
                      f"(of {len(closed_rows)} total in DB), open={'yes' if open_row else 'no'}")
        conn.close()
    except Exception as e:
        log.warning(f"Loading V3 paper state from DB failed: {e}")


V3_MAX_HOLD_MINUTES = 30   # same swing-style hold window as the S/R Probability Engine (V1) -- V3 targets structural moves, not fast scalps
V3_MAX_HOLD_MINUTES_HARD_CAP = V3_MAX_HOLD_MINUTES * 2   # absolute ceiling even while should_pause_time_exit keeps agreeing -- never truly unlimited
V3_COOLDOWN_MINUTES_AFTER_SL = 10


def _v3_entry_factor_snapshot(v3_signal):
    """Extracts the entry-time factor-component scores (see
    sr_engine_v3.analyze_oi_cluster) from whichever cluster (support or
    resistance) actually produced this trade -- for learn_adaptive_weights
    to later correlate against win/loss outcomes. Uses chosen_side straight
    from generate_v3_signal (never re-derived from direction -- see the
    "strike" field's own docstring for why that inference is wrong for
    continuation trades). Returns {} gracefully if the cluster data isn't
    there for any reason."""
    side = v3_signal.get("chosen_side")
    cluster = v3_signal.get(f"{side}_cluster") if side else None
    if not cluster or not cluster.get("tradeable_data"):
        return {}
    return {
        "strength": cluster.get("support_resistance_strength"),
        "writing": cluster.get("oi_cluster_strength"),
        "liquidity": cluster.get("liquidity_score"),
        "volume": cluster.get("volume_score"),
        "moneyness": cluster.get("moneyness_score"),
        "theta_defense": cluster.get("theta_defense_score"),
        "gamma_instability": cluster.get("gamma_instability"),
        "iv_penalty": cluster.get("iv_at_level"),
    }


def update_v3_paper_trading(symbol, v3_signal, rows, now_str, candles=None):
    """Trade lifecycle for Engine V3's OWN, separate paper-trading bucket --
    never mixed with V1/V2/Scalp's stats. Deliberately simple (fixed target/SL
    from entry), same shape as update_scalp_paper_trading -- but v3_signal is
    already a single resolved trade_decision/direction (generate_v3_signal
    picks the higher-confidence side itself), so there's no separate
    "select best candidate" step needed here.

    candles: this symbol's recent OHLC candles (oldest-first, e.g.
    market_structure_by_symbol[symbol]['recent_candles']) -- used by
    should_pause_time_exit to skip a time-exit while price action is still
    genuinely moving in the trade's favor (per 2026-08-02 request). Missing/
    too-short candle data just falls back to the plain time-exit, honestly."""
    if not state["dev_settings"]["PAPER_TRADING_ENABLED"]:
        return None
    bucket = v3_paper_trade_bucket(symbol)
    open_trade = bucket["open_trade"]

    if open_trade:
        strike, direction = open_trade["strike"], open_trade["direction"]
        row = next((r for r in rows if r.strike == strike), None)
        current_price = (row.ce_ltp if direction == "CE" else row.pe_ltp) if row else None
        if current_price:
            open_trade["current_price"] = current_price
            open_trade["points_now"] = round(current_price - open_trade["entry_price"], 2)

            held_minutes = (now_ist() - open_trade["entry_time_obj"]).total_seconds() / 60
            exit_reason = None
            if current_price >= open_trade["target_price"]:
                exit_reason = "TARGET HIT"
            elif current_price <= open_trade["sl_price"]:
                exit_reason = "STOP LOSS"
            elif held_minutes >= V3_MAX_HOLD_MINUTES_HARD_CAP:
                exit_reason = "TIME EXIT"   # absolute ceiling -- fires regardless of candle structure
            elif held_minutes >= V3_MAX_HOLD_MINUTES:
                paused = (candles and len(candles) >= 2
                          and should_pause_time_exit(direction, candles[-2], candles[-1]))
                if not paused:
                    exit_reason = "TIME EXIT"

            if exit_reason:
                points = round(current_price - open_trade["entry_price"], 2)
                open_trade.update({"exit_price": current_price, "exit_time": now_str,
                                    "exit_reason": exit_reason, "points": points})
                db_close_v3_paper_trade(open_trade.get("db_id"), current_price, now_str, exit_reason, points)
                bucket["history"].appendleft(open_trade)
                bucket["total_points"] += points
                if exit_reason == "TARGET HIT":
                    bucket["wins"] += 1
                elif exit_reason == "STOP LOSS":
                    bucket["losses"] += 1
                    state["v3_cooldown_until_by_symbol"][symbol] = now_ist() + dt.timedelta(minutes=V3_COOLDOWN_MINUTES_AFTER_SL)
                else:
                    bucket["time_exits"] += 1
                bucket["open_trade"] = None
                close_msg = f"[{now_str}] V3 PAPER TRADE CLOSED ({symbol} {strike}{direction}): {exit_reason}, {points:+.2f} pts"
                socketio.emit("alert", {"message": close_msg}, room=symbol)

    else:
        cooldown_until = state["v3_cooldown_until_by_symbol"].get(symbol)
        in_cooldown = cooldown_until and now_ist() < cooldown_until
        if not in_cooldown and v3_signal and v3_signal.get("tradeable"):
            new_trade = {
                # "strike" is the engine's own chosen entry strike -- NOT
                # inferred from direction+support/resistance_strike, which is
                # wrong for the extension/continuation cases (a CE can come
                # from resistance_strike on a breakout, a PE from
                # support_strike on a breakdown). See generate_v3_signal's
                # "strike" field docstring.
                "symbol": symbol, "strike": v3_signal["strike"],
                "direction": v3_signal["direction"],
                "entry_price": v3_signal["suggested_entry"], "target_price": v3_signal["target"],
                "sl_price": v3_signal["stop_loss"], "entry_time": now_str, "entry_time_obj": now_ist(),
                "current_price": v3_signal["suggested_entry"], "points_now": 0.0,
                "risk_reward": v3_signal.get("risk_reward"), "confidence": v3_signal.get("confidence"),
                "regime_at_entry": (v3_signal.get("regime_weights") or {}).get("label"),
                "prev_day_validation": json.dumps({
                    "support": (v3_signal.get("previous_day_validation") or {}).get("support", {}).get("status"),
                    "resistance": (v3_signal.get("previous_day_validation") or {}).get("resistance", {}).get("status"),
                }),
                # Factor-score snapshot AT ENTRY, from whichever cluster (support
                # or resistance) actually produced this trade -- feeds
                # sr_engine_v3.learn_adaptive_weights later. chosen_side comes
                # straight from generate_v3_signal, never re-derived from
                # direction (that inference is wrong for continuation trades).
                "factors_json": json.dumps(_v3_entry_factor_snapshot(v3_signal)),
            }
            new_trade["db_id"] = db_open_v3_paper_trade(symbol, new_trade)
            bucket["open_trade"] = new_trade
            open_msg = (f"[{now_str}] V3 PAPER TRADE OPENED ({symbol} {new_trade['strike']}{new_trade['direction']}) "
                        f"@ {new_trade['entry_price']} | target {new_trade['target_price']} | SL {new_trade['sl_price']} "
                        f"| R:R {new_trade.get('risk_reward')} | confidence {new_trade.get('confidence')}%")
            socketio.emit("alert", {"message": open_msg}, room=symbol)

    total_trades = bucket["wins"] + bucket["losses"] + bucket["time_exits"]
    win_rate = round(bucket["wins"] / total_trades * 100, 1) if total_trades else 0.0
    open_trade_out = None
    if bucket["open_trade"]:
        ot = bucket["open_trade"]
        open_trade_out = {k: v for k, v in ot.items() if k != "entry_time_obj"}
    return {
        "open_trade": open_trade_out,
        "history": [{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]],
        "wins": bucket["wins"], "losses": bucket["losses"], "time_exits": bucket["time_exits"],
        "win_rate": win_rate, "total_points": round(bucket["total_points"], 2),
    }


def get_v3_adaptive_weights(symbol):
    """
    Recomputes (and caches, once per calendar day per symbol) Engine V3's
    learned indicator weights from GENUINE historical paper-trade outcomes
    (sr_engine_v3.learn_adaptive_weights) -- added 2026-08-02 per request.
    Reads factors_json off already-closed v3_paper_trades rows for this
    symbol; gated on a minimum sample size inside learn_adaptive_weights
    itself, so this returns neutral defaults (with an honest diagnostic
    explaining why) until enough real trades have closed. Weights are kept
    in-memory (state) rather than persisted across restarts -- with 0 v3
    paper trades logged as of this feature's introduction, there's nothing
    to lose by recomputing from the DB on next startup regardless.
    """
    today_str = now_ist().strftime("%Y-%m-%d")
    cached = state["v3_adaptive_weights_by_symbol"].get(symbol)
    if cached and cached.get("date") == today_str:
        return cached["weights"]

    weights, diagnostics = dict(V3_DEFAULT_FACTOR_WEIGHTS), {"adjusted": False}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT exit_reason, factors_json FROM v3_paper_trades WHERE symbol=? AND status='CLOSED'",
            (symbol,),
        ).fetchall()
        conn.close()
        trade_records = []
        for r in rows:
            try:
                factors = json.loads(r["factors_json"]) if r["factors_json"] else {}
            except (json.JSONDecodeError, TypeError):
                factors = {}
            trade_records.append({"exit_reason": r["exit_reason"], "factors": factors})
        weights, diagnostics = learn_adaptive_weights(trade_records)
    except Exception as e:
        log.warning(f"get_v3_adaptive_weights failed for {symbol} (falling back to neutral defaults): {e}")

    state["v3_adaptive_weights_by_symbol"][symbol] = {"date": today_str, "weights": weights, "diagnostics": diagnostics}
    if diagnostics.get("adjusted"):
        log.info(f"V3 adaptive weights updated for {symbol}: {diagnostics}")
    return weights


def get_v3_previous_day_validation(symbol, cfg):
    """
    Computes (and caches, once per calendar day per symbol) Engine V3's
    previous-day validation -- per the confirmed approach, sourced entirely
    from the already-logged cycles/strikes/market_structure_snapshots tables
    (oi_history.db), no new snapshot infra. Reconstructs the previous trading
    day's market-structure (pivots/CPR are recomputed on the fly from that
    day's stored pdh/pdl/pdc -- both are deterministic functions of those
    three numbers, so nothing new needs to be persisted for them) and its
    LAST logged cycle as "yesterday's closing option chain".

    Returns None gracefully if there's no prior day's data yet (e.g. first
    day of collection, or DB unavailable) -- generate_v3_signal already
    degrades outcome/extension fields to UNKNOWN/not-extending in that case.
    """
    cached = state["v3_prev_day_validation_by_symbol"].get(symbol)
    today_str = now_ist().strftime("%Y-%m-%d")
    if cached and cached.get("date") == today_str:
        return cached["result"]

    try:
        from market_structure import classical_pivots, calc_cpr
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        snap = conn.execute(
            "SELECT * FROM market_structure_snapshots WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
            (symbol, today_str),
        ).fetchone()
        if not snap or not (snap["pdh"] and snap["pdl"] and snap["pdc"]):
            conn.close()
            return None
        prev_date = snap["date"]

        last_cycle = conn.execute(
            "SELECT * FROM cycles WHERE symbol=? AND date=? ORDER BY ts DESC LIMIT 1",
            (symbol, prev_date),
        ).fetchone()
        if not last_cycle:
            conn.close()
            return None

        strike_rows = conn.execute("SELECT * FROM strikes WHERE cycle_id=?", (last_cycle["id"],)).fetchall()
        conn.close()
        if not strike_rows:
            return None

        prev_rows = [
            StrikeRow(
                strike=s["strike"], ce_oi=s["ce_oi"] or 0, ce_oi_chg=s["ce_oi_chg"] or 0, ce_vol=s["ce_vol"] or 0,
                ce_ltp=s["ce_ltp"] or 0.0, ce_iv=s["ce_iv"] or 0.0, ce_delta=s["ce_delta"] or 0.0,
                pe_oi=s["pe_oi"] or 0, pe_oi_chg=s["pe_oi_chg"] or 0, pe_vol=s["pe_vol"] or 0,
                pe_ltp=s["pe_ltp"] or 0.0, pe_iv=s["pe_iv"] or 0.0, pe_delta=s["pe_delta"] or 0.0,
            ) for s in strike_rows
        ]
        # buy/sell quantity isn't persisted to this DB (live-only, NSE order-book
        # field) -- analyze_oi_cluster degrades liquidity_score to None honestly
        # for this previous-day path, per sr_engine_v3.py's module docstring.

        prev_ms = {
            "regime": snap["regime"], "vwap": snap["vwap"],
            "swing_high": snap["swing_high"], "swing_low": snap["swing_low"],
            "pivots": classical_pivots(snap["pdh"], snap["pdl"], snap["pdc"]),
            "cpr": calc_cpr(snap["pdh"], snap["pdl"], snap["pdc"]),
            "custom_levels": json.loads(snap["custom_levels_json"]) if snap["custom_levels_json"] else None,
        }
        result = validate_previous_day_levels(
            prev_rows, prev_ms, last_cycle["underlying_ltp"], cfg["step"],
            factor_weights=get_v3_adaptive_weights(symbol),
        )
        state["v3_prev_day_validation_by_symbol"][symbol] = {"date": today_str, "result": result}
        return result
    except Exception as e:
        log.warning(f"get_v3_previous_day_validation failed for {symbol}: {e}")
        return None


def get_v3_today_ltp_history(symbol, underlying):
    """Growing, day-scoped underlying-LTP buffer for classify_level_outcome
    (Held/Broke/Flipped needs to know whether price EVER crossed yesterday's
    level today -- the general-purpose `history` deque is capped at
    MAX_HISTORY_POINTS=200 cycles, which at REFRESH_INTERVAL=1s for the active
    symbol is only ~3 minutes, nowhere near a full session). Resets at the
    calendar-day boundary; capped at a generous 5000 points so memory stays
    bounded even on a very-fast-refreshing symbol."""
    today_str = now_ist().strftime("%Y-%m-%d")
    bucket = state["v3_today_ltp_by_symbol"].get(symbol)
    if not bucket or bucket["date"] != today_str:
        bucket = {"date": today_str, "ltps": deque(maxlen=5000)}
        state["v3_today_ltp_by_symbol"][symbol] = bucket
    if underlying is not None:
        bucket["ltps"].append(underlying)
    return list(bucket["ltps"])


def get_v3_volume_history(symbol, strike, direction, current_volume):
    """Rolling per-(strike,direction) volume buffer for V3's extension-
    detection volume-expansion check -- returns PRIOR readings only (same
    contract as sr_probability_engine.compute_volume_expansion's callers),
    THEN appends this cycle's reading for next time."""
    bucket = state["v3_volume_history_by_symbol"].setdefault(symbol, {})
    key = (strike, direction)
    history = bucket.setdefault(key, deque(maxlen=30))
    prior = list(history)
    if current_volume is not None:
        history.append(current_volume)
    return prior


def update_paper_orders(symbol, rows, now_str, cfg, candles=None):
    """
    candles: this symbol's recent OHLC candles (oldest-first, e.g.
    state["recent_candles_by_symbol"][symbol]) -- passed through to the
    AUTO-SWING time-exit branch below so it can share update_paper_trading's
    should_pause_time_exit guard instead of force-closing purely on the
    clock while price is still moving in the trade's favor.

    Per-cycle background pass for THIS symbol's paper orders -- MANUAL and
    AUTO both live in the same `paper_orders` table and are managed
    IDENTICALLY here; `trade_source` only distinguishes who/what opened the
    order, never how it's filled/exited. (Entry-side fan-out for AUTO orders
    is a separate concern -- see fanout_auto_trade_entry -- this function only
    fills already-PENDING orders and manages already-OPEN ones.) Unlike
    update_paper_trading/update_scalp_paper_trading (each own a single
    in-memory bucket per symbol), paper orders are genuinely multi-user and
    DB-backed only -- there's no in-memory state here to get out of sync.

    Three jobs per cycle:

    1. Fill PENDING orders: LIMIT (current price <= limit_price, at-or-better)
       and STOP (current price >= stop_price -- a breakout trigger, the
       OPPOSITE direction check from LIMIT, matching a real broker's SL-M).
       The wallet debit happens HERE, atomically, against the ACTUAL fill
       price and CURRENT balance (billing.debit_if_sufficient) -- never at
       placement time, so a fill can never silently overdraw the wallet even
       if OTHER orders consumed the balance since this one was placed. If
       insufficient, the order just stays PENDING and is retried next cycle.

    2. Auto-exit OPEN trades on target/SL, with a generalized trailing stop
       (ported from the Swing engine's 2-stage breakeven-then-trail
       algorithm, applied per-row here -- falls back to the global
       BREAKEVEN_TRIGGER_PCT/TRAIL_TRIGGER_PCT/TRAIL_GIVEBACK_PCT constants
       when a row doesn't specify its own).

    3. Forced intraday square-off for BRACKET/COVER orders (intraday_only=1),
       INTRADAY_SQUAREOFF_BUFFER_MINUTES before the symbol's close. This is
       one more branch in the SAME exit-reason cascade as target/SL/trailing
       below -- deliberately NOT a second, independent code path/timer racing
       the same row, which would reintroduce exactly the double-close hazard
       the rowcount-guard pattern exists to prevent.

    Every status-changing UPDATE here is a single atomic `WHERE status=<expected>`
    statement with a rowcount check -- this is what stops this background pass
    and a user's own concurrent /api/manual-trade/exit call from double-closing
    (and double wallet-crediting) the same order; whichever write lands first
    wins, the other sees rowcount=0 and does nothing further.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pending = conn.execute(
            "SELECT * FROM paper_orders WHERE symbol=? AND status='PENDING'", (symbol,)
        ).fetchall()
        for t in pending:
            row = next((r for r in rows if r.strike == t["strike"]), None)
            current_price = (row.ce_ltp if t["direction"] == "CE" else row.pe_ltp) if row else None
            if not current_price:
                continue
            order_type = t["order_type"] or "LIMIT"
            if order_type == "STOP":
                if t["stop_price"] is None or current_price < t["stop_price"]:
                    continue
            else:   # LIMIT (and a LIMIT-style BRACKET entry, if placed with a limit_price)
                if t["limit_price"] is None or current_price > t["limit_price"]:
                    continue
            new_balance = billing.debit_if_sufficient(
                t["user_id"], round(current_price * t["qty"], 2), "trade_entry",
                note=f"{symbol} {t['strike']}{t['direction']} {order_type.lower()} fill",
            )
            if new_balance is None:
                continue   # stays PENDING, retried next cycle -- no error surfaced
            cur = conn.execute(
                "UPDATE paper_orders SET status='OPEN', entry_price=?, entry_time=?, entry_ts=? "
                "WHERE id=? AND status='PENDING'",
                (current_price, now_str, now_ist().timestamp(), t["id"]),
            )
            conn.commit()
            if cur.rowcount:
                socketio.emit(
                    "alert",
                    {"message": f"{order_type} ORDER FILLED: {symbol} {t['strike']}{t['direction']} @ {current_price}"},
                    room=f"user_{t['user_id']}",
                )

        open_trades = conn.execute(
            "SELECT * FROM paper_orders WHERE symbol=? AND status='OPEN'", (symbol,)
        ).fetchall()
        now = now_ist()
        oh, om, ch, cm = _resolve_market_hours(cfg, now)
        squareoff_from = now.replace(hour=ch, minute=cm, second=0, microsecond=0) - dt.timedelta(minutes=INTRADAY_SQUAREOFF_BUFFER_MINUTES)
        near_close = now >= squareoff_from

        for t in open_trades:
            row = next((r for r in rows if r.strike == t["strike"]), None)
            current_price = (row.ce_ltp if t["direction"] == "CE" else row.pe_ltp) if row else None
            if not current_price:
                continue

            # -- Generalized trailing SL: same 2-stage algorithm as the Swing
            # engine's own (untouched) update_paper_trading, applied per-row
            # here so ANY order (manual or auto, any order type) can opt in. --
            sl_price = t["sl_price"]
            sl_trailed = bool(t["sl_trailed"])
            peak_price = t["peak_price"]
            sl_changed = peak_changed = trailed_changed = False
            if t["trailing_stop_enabled"] and t["target_price"] is not None:
                entry = t["entry_price"]
                target = t["target_price"]
                prior_peak = peak_price if peak_price is not None else entry
                peak = max(prior_peak, current_price)
                if peak != prior_peak:
                    peak_price, peak_changed = peak, True
                target_distance = target - entry
                if target_distance > 0:
                    trigger_pct = t["trailing_trigger_pct"] if t["trailing_trigger_pct"] is not None else TRAIL_TRIGGER_PCT
                    giveback_pct = t["trailing_giveback_pct"] if t["trailing_giveback_pct"] is not None else TRAIL_GIVEBACK_PCT
                    breakeven_pct = t["breakeven_trigger_pct"] if t["breakeven_trigger_pct"] is not None else BREAKEVEN_TRIGGER_PCT
                    progress_pct = (peak - entry) / target_distance
                    cur_sl = sl_price if sl_price is not None else -float("inf")
                    if progress_pct >= trigger_pct:
                        candidate_sl = round(entry + (peak - entry) * (1 - giveback_pct), 2)
                        if candidate_sl > cur_sl:
                            sl_price, sl_trailed = candidate_sl, True
                            sl_changed = trailed_changed = True
                    elif progress_pct >= breakeven_pct and cur_sl < entry:
                        sl_price, sl_trailed = entry, True
                        sl_changed = trailed_changed = True

            exit_reason = None
            if t["target_price"] is not None and current_price >= t["target_price"]:
                exit_reason = "TARGET HIT"
            elif sl_price is not None and current_price <= sl_price:
                exit_reason = "TRAILING SL" if sl_trailed else "STOP LOSS"
            elif t["intraday_only"] and near_close:
                exit_reason = "SQUARE-OFF"
            elif t["trade_source"] == "AUTO" and t["source_engine"] == "SWING" and t["entry_ts"]:
                # Same tuned max_hold_minutes the SWING reference trade already
                # time-exits on (update_paper_trading) -- previously these
                # per-user rows had no time-exit at all and could sit open
                # indefinitely waiting for target/SL.
                held_minutes = (now.timestamp() - t["entry_ts"]) / 60
                max_hold = get_sr_live_params(symbol)["max_hold_minutes"]
                if held_minutes >= max_hold * 2:
                    exit_reason = "TIME EXIT"   # absolute ceiling -- fires regardless of candle structure
                elif held_minutes >= max_hold:
                    paused = (candles and len(candles) >= 2
                              and should_pause_time_exit(t["direction"], candles[-2], candles[-1]))
                    if not paused:
                        exit_reason = "TIME EXIT"

            if not exit_reason:
                if sl_changed or peak_changed or trailed_changed:
                    conn.execute(
                        "UPDATE paper_orders SET sl_price=?, peak_price=?, sl_trailed=? WHERE id=? AND status='OPEN'",
                        (sl_price, peak_price, 1 if sl_trailed else 0, t["id"]),
                    )
                    conn.commit()
                continue

            points = round(current_price - t["entry_price"], 2)
            cur = conn.execute(
                "UPDATE paper_orders SET exit_price=?, exit_time=?, exit_reason=?, points=?, status='CLOSED', "
                "sl_price=?, peak_price=?, sl_trailed=? WHERE id=? AND status='OPEN'",
                (current_price, now_str, exit_reason, points, sl_price, peak_price, 1 if sl_trailed else 0, t["id"]),
            )
            conn.commit()
            if not cur.rowcount:
                continue   # lost the race to a concurrent manual exit -- do NOT double-credit
            if t["wallet_linked"]:
                # Credit the FULL exit proceeds (current_price*qty), not just
                # points*qty -- entry already debited the full entry cost
                # (entry_price*qty), so crediting only the net points here
                # would double-count that debit instead of returning
                # principal+P&L. Net effect over the round trip is still
                # exactly points*qty, same as the P&L shown in the alert.
                billing.create_wallet_transaction(
                    t["user_id"], round(current_price * t["qty"], 2), "trade_pnl",
                    note=f"{symbol} {t['strike']}{t['direction']} {exit_reason}",
                )
            if exit_reason in ("STOP LOSS", "TRAILING SL") and t["trade_source"] == "AUTO" and t["source_engine"]:
                if t["source_engine"] == "SCALP":
                    cooldown_minutes = SCALP_COOLDOWN_MINUTES_AFTER_SL
                elif t["source_engine"] == "SWING":
                    cooldown_minutes = get_sr_live_params(symbol)["cooldown_minutes_after_sl"]
                else:
                    cooldown_minutes = COOLDOWN_MINUTES_AFTER_SL
                state["auto_fanout_cooldown"][(t["user_id"], t["source_engine"], symbol)] = now + dt.timedelta(minutes=cooldown_minutes)
            label = f"AUTO TRADE ({t['source_engine']})" if t["trade_source"] == "AUTO" else "MANUAL TRADE"
            socketio.emit(
                "alert",
                {"message": f"{label} AUTO-EXIT: {symbol} {t['strike']}{t['direction']} {exit_reason} "
                            f"@ {current_price} ({points:+.2f} pts)"},
                room=f"user_{t['user_id']}",
            )
    finally:
        conn.close()


def fanout_auto_trade_entry(engine, symbol, cfg, trigger, now):
    """
    Per-user AI Auto-Trading fan-out: called on every genuine trigger firing
    for `engine` ('SWING'|'SCALP') on `symbol`. For every subscriber who has
    opted in (user_auto_trading_settings.enabled=1 for this engine), opens an
    INDEPENDENT paper_orders row against THEIR OWN wallet -- trade_source=
    'AUTO', source_engine=engine. Never touches the engine's own unchanged
    system-wide reference trade (update_paper_trading/update_scalp_paper_trading)
    -- this is purely additive, one more consumer of the same trigger.

    Both guards below are REQUIRED correctness, not polish:
    - Per-(user, engine, symbol) cooldown after an AUTO SL exit
      (state["auto_fanout_cooldown"], set in update_paper_orders) -- same
      idea as the engines' own (unchanged) cooldown trackers, just scoped
      per-user instead of system-wide.
    - Skip if the user ALREADY has an OPEN or PENDING paper_orders row for
      this (symbol, source_engine). Swing's sr_trigger is edge-triggered
      (self-gates via its own trade_opened flag, fires once per genuine new
      setup) but Scalp's tradeable candidate is NOT -- it can stay true for
      many consecutive 1-second cycles. Without this guard, calling this
      function every cycle a Scalp candidate remains tradeable would open a
      brand-new AUTO order for every enrolled user on EVERY cycle. Applied
      uniformly to both engines (defense-in-depth for Swing too, even though
      its own trigger is already edge-triggered).

    `trigger` is either engine's per-cycle candidate dict -- both shapes
    already share the fields used here (strike, direction, entry_price,
    target_price, sl_price): get_sr_trade_trigger's return value for SWING,
    select_best_scalp_candidate's for SCALP.

    user_auto_trading_settings.qty is LOTS, same convention as the Manual
    Trading page's qty-input -- multiplied here by THIS symbol's LOT_SIZES
    entry to get the actual order quantity, so a subscriber's AUTO orders
    are sized consistently with what they'd place manually for that symbol.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        enrolled = conn.execute(
            "SELECT user_id, qty FROM user_auto_trading_settings WHERE engine=? AND enabled=1", (engine,)
        ).fetchall()
        if not enrolled:
            return
        if len(enrolled) > MAX_AUTO_TRADE_FANOUT_USERS:
            log.warning(f"AUTO fan-out ({engine} {symbol}): {len(enrolled)} enrolled users exceeds "
                        f"MAX_AUTO_TRADE_FANOUT_USERS={MAX_AUTO_TRADE_FANOUT_USERS} -- skipping the excess "
                        f"(see the batched-transaction seam noted at MAX_AUTO_TRADE_FANOUT_USERS's definition).")
            enrolled = enrolled[:MAX_AUTO_TRADE_FANOUT_USERS]

        strike, direction = trigger["strike"], trigger["direction"]
        entry_price = trigger["entry_price"]
        target_price = trigger.get("target_price")
        sl_price = trigger.get("sl_price")
        now_str = now.strftime("%H:%M:%S")

        # SWING orders opt into the same tuned trailing-stop behavior as the
        # engine's own reference trade (update_paper_trading) -- previously
        # these per-user rows only ever got a fixed target/SL, never trailed.
        # SCALP is untouched (its own engine, no backtest_profiles tuning).
        if engine == "SWING":
            live_params = get_sr_live_params(symbol)
            trailing_trigger_pct = live_params["trail_trigger_pct"]
            trailing_giveback_pct = live_params["trail_giveback_pct"]
            breakeven_trigger_pct = live_params["breakeven_trigger_pct"]
        else:
            trailing_trigger_pct = trailing_giveback_pct = breakeven_trigger_pct = None

        for u in enrolled:
            uid, lots = u["user_id"], max(1, int(u["qty"] or 1))
            # user_auto_trading_settings.qty is entered as LOTS (same convention as
            # the Manual Trading page's qty-input, see LOT_SIZES's definition) --
            # multiply by THIS symbol's lot size to get the actual tradeable
            # quantity. Previously this was used raw as the actual qty, so an
            # engine watching multiple symbols (different lot sizes: NIFTY=65,
            # BANKNIFTY=30, SENSEX=20, ...) silently under/over-sized every AUTO
            # order instead of matching what Manual Trading would place for the
            # same number of lots.
            qty = lots * LOT_SIZES.get(symbol, 1)
            cooldown_until = state["auto_fanout_cooldown"].get((uid, engine, symbol))
            if cooldown_until and now < cooldown_until:
                continue
            existing = conn.execute(
                "SELECT 1 FROM paper_orders WHERE user_id=? AND symbol=? AND source_engine=? "
                "AND status IN ('OPEN','PENDING')", (uid, symbol, engine),
            ).fetchone()
            if existing:
                continue   # already has a live AUTO order for this (user, symbol, engine) -- required dedup guard, see docstring
            new_balance = billing.debit_if_sufficient(
                uid, round(entry_price * qty, 2), "trade_entry",
                note=f"{symbol} {strike}{direction} AUTO ({engine}) entry",
            )
            if new_balance is None:
                log.info(f"AUTO fan-out ({engine} {symbol}): user_id={uid} skipped -- insufficient balance.")
                continue
            conn.execute(
                """INSERT INTO paper_orders (user_id, symbol, strike, direction, trade_source, source_engine,
                                              order_type, entry_price, target_price, sl_price, qty,
                                              trailing_stop_enabled, trailing_trigger_pct, trailing_giveback_pct,
                                              breakeven_trigger_pct,
                                              entry_time, entry_ts, status, wallet_linked)
                   VALUES (?,?,?,?, 'AUTO', ?, 'MARKET', ?,?,?,?, ?,?,?,?, ?,?, 'OPEN', 1)""",
                (uid, symbol, strike, direction, engine, entry_price, target_price, sl_price, qty,
                 1 if engine == "SWING" else 0, trailing_trigger_pct, trailing_giveback_pct, breakeven_trigger_pct,
                 now_str, now.timestamp()),
            )
            conn.commit()
            log.info(f"AUTO TRADE OPENED ({engine}) | user_id={uid} {symbol} {strike}{direction} @ {entry_price} "
                     f"x{qty} ({lots} lot(s) x {LOT_SIZES.get(symbol, 1)}) | Target={target_price} SL={sl_price} | "
                     f"wallet -> {new_balance:.2f}")
            socketio.emit(
                "alert",
                {"message": f"AUTO TRADE OPENED ({engine}): {symbol} {strike}{direction} @ {entry_price} "
                            f"| target {target_price} | SL {sl_price}"},
                room=f"user_{uid}",
            )
    finally:
        conn.close()


def update_paper_trading(symbol, signal, rows, now_str, sr_trigger=None, candles=None):
    """candles: this symbol's recent OHLC candles (oldest-first, e.g.
    state["recent_candles_by_symbol"][symbol]) -- used by should_pause_time_exit
    to skip a time-exit while price action is still genuinely moving in the
    trade's favor, same guard V3 already has (per 2026-08-02 request). Missing/
    too-short candle data just falls back to the plain time-exit."""
    if not state["dev_settings"]["PAPER_TRADING_ENABLED"]:
        return None
    live_params = get_sr_live_params(symbol)
    bucket = paper_trade_bucket(symbol)
    open_trade = bucket["open_trade"]

    if open_trade:
        strike, direction = open_trade["strike"], open_trade["direction"]
        row = next((r for r in rows if r.strike == strike), None)
        current_price = (row.ce_ltp if direction == "CE" else row.pe_ltp) if row else None
        if current_price:
            open_trade["current_price"] = current_price
            open_trade["points_now"] = round(current_price - open_trade["entry_price"], 2)

            # -- Trailing SL: 2-stage, standard institutional scalping practice --
            # Stage 1 (BREAKEVEN_TRIGGER_PCT of the way to target): move SL up to
            #   entry price -- from here on, worst case is a scratch, not a loss.
            # Stage 2 (TRAIL_TRIGGER_PCT of the way to target): trail SL behind the
            #   peak price reached, locking in (1 - TRAIL_GIVEBACK_PCT) of the peak
            #   gain. SL only ever moves favorably (up for CE/PE both, since both
            #   are "more premium = more profit"), never back down.
            entry = open_trade["entry_price"]
            target = open_trade["target_price"]
            peak = max(open_trade.get("peak_price", entry), current_price)
            open_trade["peak_price"] = peak
            target_distance = target - entry
            if state["dev_settings"]["TRAILING_SL_ENABLED"] and target_distance > 0:
                progress_pct = (peak - entry) / target_distance
                if progress_pct >= live_params["trail_trigger_pct"]:
                    locked_gain = (peak - entry) * (1 - live_params["trail_giveback_pct"])
                    new_sl = round(entry + locked_gain, 2)
                    if new_sl > open_trade["sl_price"]:
                        open_trade["sl_price"] = new_sl
                        open_trade["sl_trailed"] = True
                elif progress_pct >= live_params["breakeven_trigger_pct"] and open_trade["sl_price"] < entry:
                    open_trade["sl_price"] = entry
                    open_trade["sl_trailed"] = True

            # Milestone 18: throttled "still open" update to the public
            # signals channel -- DISCONNECTED as of Milestone 19: the
            # public channel now receives signals ONLY from the Trading
            # Intelligence engine (agents/trading_intelligence/
            # telegram_notifier.py, wired from api.run_scheduled_cycle()),
            # never from the S/R Engine's own trade lifecycle. Formatter
            # kept below (format_signal_progress_message) and
            # send_telegram_channel() itself kept (still a real, general
            # utility) -- only this call site is stopped, per explicit
            # instruction not to delete the underlying tracking logic.
            if target_distance > 0:
                progress_bucket = max(0, min(100, int((current_price - entry) / target_distance * 100) // 25 * 25))
                open_trade["_telegram_progress_bucket"] = progress_bucket

            held_minutes = (dt.datetime.now() - open_trade["entry_time_obj"]).total_seconds() / 60
            exit_reason = None
            if current_price >= open_trade["target_price"]:
                exit_reason = "TARGET HIT"
            elif current_price <= open_trade["sl_price"]:
                exit_reason = "TRAILING SL" if open_trade.get("sl_trailed") else "STOP LOSS"
            elif held_minutes >= live_params["max_hold_minutes"] * 2:
                exit_reason = "TIME EXIT"   # absolute ceiling -- fires regardless of candle structure
            elif held_minutes >= live_params["max_hold_minutes"]:
                paused = (candles and len(candles) >= 2
                          and should_pause_time_exit(direction, candles[-2], candles[-1]))
                if not paused:
                    exit_reason = "TIME EXIT"
            elif (state["dev_settings"]["STAGNANT_EXIT_ENABLED"] and held_minutes >= STAGNANT_EXIT_MINUTES
                  and target_distance > 0 and abs(open_trade["points_now"]) < target_distance * STAGNANT_EXIT_THRESHOLD_PCT):
                # Genuinely going nowhere (e.g. a NATURALGAS-style flat grind) -- cut
                # early instead of bleeding theta decay for the full MAX_HOLD_MINUTES.
                exit_reason = "STAGNANT EXIT"

            if exit_reason:
                points = round(current_price - open_trade["entry_price"], 2)
                open_trade.update({
                    "exit_price": current_price, "exit_time": now_str,
                    "exit_reason": exit_reason, "points": points,
                    "pnl": round(points * PAPER_TRADE_LOT_QTY, 2),
                })
                db_close_paper_trade(open_trade.get("db_id"), current_price, now_str, exit_reason, points)
                bucket["history"].appendleft(open_trade)
                bucket["total_points"] += points
                if exit_reason == "TARGET HIT":
                    bucket["wins"] += 1
                    state["consecutive_time_exits_by_symbol"][symbol] = 0
                elif exit_reason in ("STOP LOSS", "TRAILING SL"):
                    bucket["losses"] += 1
                    state["consecutive_time_exits_by_symbol"][symbol] = 0
                else:
                    bucket["time_exits"] += 1
                    tcount = state["consecutive_time_exits_by_symbol"].get(symbol, 0) + 1
                    state["consecutive_time_exits_by_symbol"][symbol] = tcount
                bucket["open_trade"] = None
                if exit_reason == "STOP LOSS":
                    state["cooldown_until_by_symbol"][symbol] = dt.datetime.now() + dt.timedelta(minutes=live_params["cooldown_minutes_after_sl"])
                elif exit_reason == "TIME EXIT" and state["consecutive_time_exits_by_symbol"].get(symbol, 0) >= TIME_EXIT_COOLDOWN_THRESHOLD:
                    state["cooldown_until_by_symbol"][symbol] = dt.datetime.now() + dt.timedelta(minutes=live_params["cooldown_minutes_after_timeout"])
                    state["consecutive_time_exits_by_symbol"][symbol] = 0
                    log.info(f"{symbol}: {TIME_EXIT_COOLDOWN_THRESHOLD} consecutive time-exits (market too flat to resolve) -- cooling down {COOLDOWN_MINUTES_AFTER_TIMEOUT}min before re-entry.")
                close_msg = f"[{now_str}] PAPER TRADE CLOSED ({symbol} {strike}{direction}): {exit_reason}, {points:+.2f} pts"
                socketio.emit("alert", {"message": close_msg})
                emoji = "\U0001F7E2" if exit_reason == "TARGET HIT" else ("\U0001F534" if exit_reason == "STOP LOSS" else "\U000023F1")
                send_telegram(f"{emoji} {close_msg}")
                # Milestone 19: S/R Engine exit -> public-channel post
                # disconnected -- see the progress-update block above for
                # why (channel now sourced from Trading Intelligence only).

    elif sr_trigger:
        new_trade = {
            "symbol": symbol, "strike": sr_trigger["strike"], "direction": sr_trigger["direction"],
            "entry_price": sr_trigger["entry_price"], "target_price": sr_trigger["target_price"],
            "sl_price": sr_trigger["sl_price"], "entry_time": now_str,
            "entry_time_obj": dt.datetime.now(), "confidence": sr_trigger["confidence"],
            "current_price": sr_trigger["entry_price"], "points_now": 0.0,
            "source": "sr_engine", "sr_level": sr_trigger.get("level_key"),
            "institutional_score": sr_trigger.get("institutional_score"),
            "institutional_tier": sr_trigger.get("institutional_tier"),
            "regime_at_entry": sr_trigger.get("regime_at_entry"),
            "risk_reward": sr_trigger.get("risk_reward"),
        }
        new_trade["db_id"] = db_open_paper_trade(symbol, new_trade)
        bucket["open_trade"] = new_trade
        open_msg = (f"[{now_str}] PAPER TRADE OPENED via S/R Engine ({symbol} {sr_trigger['strike']}{sr_trigger['direction']}, "
                    f"{sr_trigger.get('level_key','')}) @ {sr_trigger['entry_price']} | target {sr_trigger['target_price']} | SL {sr_trigger['sl_price']}")
        socketio.emit("alert", {"message": open_msg})
        send_telegram(f"\U0001F4C8 {open_msg}")
        # Milestone 19: S/R Engine entry -> public-channel post
        # disconnected -- see progress-update block above for why.

    total_trades = bucket["wins"] + bucket["losses"] + bucket["time_exits"]
    win_rate = round(bucket["wins"] / total_trades * 100, 1) if total_trades else 0.0

    open_trade_out = None
    if bucket["open_trade"]:
        ot = bucket["open_trade"]
        open_trade_out = {k: v for k, v in ot.items() if k != "entry_time_obj"}

    return {
        "open_trade": open_trade_out,
        "history": [{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]],
        "wins": bucket["wins"], "losses": bucket["losses"], "time_exits": bucket["time_exits"],
        "win_rate": win_rate, "total_points": round(bucket["total_points"], 2),
    }


# ----------------------------------------------------------------------------
# HISTORICAL DATA LOGGING (SQLite) -- this is what backtest.py replays later.
# Every live cycle gets appended here, so backtesting only works on data
# collected from the point this feature was turned on onwards.
# ----------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # WAL mode: lets readers (backtest.py CLI, /backtest, /calibration pages)
    # run concurrently with the live app's continuous writes instead of
    # blocking on SQLite's default rollback-journal locking. This is stored
    # persistently in the DB file itself -- only needs to run once, but is
    # harmless (and fast) to set on every startup.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")   # if a lock is briefly held, wait up to 5s instead of failing immediately
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, ts TEXT, date TEXT, time TEXT,
            underlying_ltp REAL, atm INTEGER, pcr REAL, max_pain INTEGER,
            bias TEXT, note TEXT,
            signal_action TEXT, signal_strike INTEGER, signal_direction TEXT,
            signal_entry REAL, signal_target REAL, signal_sl REAL, signal_confidence INTEGER,
            signal_tradeable INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strikes (
            cycle_id INTEGER, strike INTEGER,
            ce_oi INTEGER, ce_oi_chg INTEGER, ce_vol INTEGER, ce_ltp REAL, ce_chg_pct REAL, ce_signal TEXT, ce_iv REAL,
            ce_delta REAL, ce_gamma REAL, ce_theta REAL, ce_vega REAL,
            pe_oi INTEGER, pe_oi_chg INTEGER, pe_vol INTEGER, pe_ltp REAL, pe_chg_pct REAL, pe_signal TEXT, pe_iv REAL,
            pe_delta REAL, pe_gamma REAL, pe_theta REAL, pe_vega REAL,
            FOREIGN KEY(cycle_id) REFERENCES cycles(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cycles_symbol_date ON cycles(symbol, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strikes_cycle_id ON strikes(cycle_id)")

    # Safe migration: add IV-tracking columns to strikes if they don't already
    # exist. Angel One's live option-chain already includes ce_iv/pe_iv (see
    # StrikeRow) -- we just weren't PERSISTING them historically until now.
    # This starts genuine IV data-collection going forward; Greeks-based
    # analysis (Gamma/Theta) should only be attempted once enough real
    # historical IV data has accumulated (see project notes -- no fabricated
    # Greeks without real underlying data).
    existing_strike_cols = {row[1] for row in conn.execute("PRAGMA table_info(strikes)")}
    for col in ("ce_iv", "pe_iv", "ce_delta", "ce_gamma", "ce_theta", "ce_vega", "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
        if col not in existing_strike_cols:
            conn.execute(f"ALTER TABLE strikes ADD COLUMN {col} REAL")
            log.info(f"Migrated strikes: added column '{col}' -- IV/Greeks data-collection starts now.")
    # -- Accounts / Roles / Subscriptions / Wallet -------------------------
    # MUST run before the manual_paper_trades migration below, which queries
    # `users` (to backfill legacy rows to the bootstrap admin) -- on a DB
    # that has an old-shaped manual_paper_trades table but no `users` table
    # yet (a genuinely fresh upgrade path), querying `users` before it exists
    # would raise sqlite3.OperationalError. Verified by test_manual_trading.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            username TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'subscriber',
            is_verified INTEGER NOT NULL DEFAULT 0,
            verification_token_hash TEXT,
            verification_token_expires_at TEXT,
            is_suspended INTEGER NOT NULL DEFAULT 0,
            trial_started_at TEXT,
            trial_ends_at TEXT,
            subscription_plan TEXT,
            subscription_expires_at TEXT,
            wallet_balance REAL NOT NULL DEFAULT 50000,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(created_by_user_id) REFERENCES users(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_id ON wallet_transactions(user_id)")

    # One-time bootstrap admin -- ONLY created if no admin-role user exists yet
    # (idempotent: safe to leave ADMIN_BOOTSTRAP_* set in .env permanently).
    # Credentials come from .env, never hardcoded here -- same convention this
    # file already uses for every other secret (Angel One creds, Telegram, etc).
    existing_admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not existing_admin:
        if ADMIN_BOOTSTRAP_USERNAME and ADMIN_BOOTSTRAP_PASSWORD:
            now_str = now_ist().isoformat()
            conn.execute(
                """INSERT INTO users (email, username, password_hash, role, is_verified, is_suspended,
                                       wallet_balance, created_at, updated_at)
                   VALUES (?,?,?,?,1,0,?,?,?)""",
                (ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_USERNAME, auth.hash_password(ADMIN_BOOTSTRAP_PASSWORD),
                 "admin", billing.DEFAULT_WALLET_BALANCE, now_str, now_str),
            )
            log.info(f"Bootstrap admin user '{ADMIN_BOOTSTRAP_USERNAME}' created (no admin row existed yet).")
        else:
            log.warning("No admin user exists and ADMIN_BOOTSTRAP_USERNAME/PASSWORD are unset in .env -- "
                        "no one can log in as admin until a row is created.")
    else:
        log.info("Admin user already exists -- skipping bootstrap creation (idempotent).")

    # Phase 3: rename manual_paper_trades -> paper_orders (it now holds BOTH
    # manual AND AI-auto orders, distinguished by trade_source -- see below).
    # NOTE: this table is one character away from the separate, UNRELATED,
    # symbol-scoped `paper_trades` table (the Swing engine's own system-wide
    # reference-trade tracking, powering /calibration -- deliberately left
    # untouched by Phase 3). Do not confuse the two when editing this file.
    _existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "manual_paper_trades" in _existing_tables and "paper_orders" not in _existing_tables:
        conn.execute("ALTER TABLE manual_paper_trades RENAME TO paper_orders")
        conn.execute("DROP INDEX IF EXISTS idx_manual_trades_user_status")
        log.info("Migrated manual_paper_trades -> paper_orders (Phase 3: unified manual+AI-auto orders).")

    existing_manual_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_orders)")}
    if existing_manual_cols and "qty" not in existing_manual_cols:
        conn.execute("ALTER TABLE paper_orders ADD COLUMN qty INTEGER DEFAULT 1")
        log.info("Migrated paper_orders: added column 'qty'.")
    if existing_manual_cols and "user_id" not in existing_manual_cols:
        # Phase 2: per-user partitioning + limit orders + wallet linkage.
        # Legacy rows predate BOTH concepts -- backfilled to the current admin
        # (so they stay visible to whoever actually made them, not orphaned)
        # and flagged wallet_linked=0 so they can NEVER trigger a wallet
        # debit/credit (crediting them now would be a phantom balance change
        # with no matching original entry, since nothing was ever debited).
        conn.execute("ALTER TABLE paper_orders ADD COLUMN user_id INTEGER")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN order_type TEXT DEFAULT 'MARKET'")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN limit_price REAL")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN wallet_linked INTEGER DEFAULT 1")
        admin_row = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        admin_id = admin_row[0] if admin_row else None
        cur = conn.execute(
            "UPDATE paper_orders SET user_id=?, wallet_linked=0, order_type='MARKET' WHERE user_id IS NULL",
            (admin_id,),
        )
        log.info(f"Migrated paper_orders: added user_id/order_type/limit_price/wallet_linked; "
                 f"backfilled {cur.rowcount} legacy row(s) -> user_id={admin_id}, wallet_linked=0.")
        if admin_id is None and cur.rowcount:
            log.warning("Legacy paper_orders rows backfilled with user_id=NULL (no admin user "
                        "existed yet) -- these will be invisible to any per-user query until fixed manually.")
    if existing_manual_cols and "trade_source" not in existing_manual_cols:
        # Phase 3: unify with AI Auto-Trading (per-user opt-in, see
        # user_auto_trading_settings + fanout_auto_trade_entry below).
        # trade_source is ALWAYS exactly 'MANUAL' or 'AUTO' -- never a third
        # value; which AI engine produced an AUTO order lives separately in
        # source_engine so trade_source itself never needs a third value.
        conn.execute("ALTER TABLE paper_orders ADD COLUMN trade_source TEXT NOT NULL DEFAULT 'MANUAL'")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN source_engine TEXT")             # NULL | 'SWING' | 'SCALP'
        conn.execute("ALTER TABLE paper_orders ADD COLUMN stop_price REAL")                # STOP order trigger
        conn.execute("ALTER TABLE paper_orders ADD COLUMN trailing_stop_enabled INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN trailing_trigger_pct REAL")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN trailing_giveback_pct REAL")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN breakeven_trigger_pct REAL")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN peak_price REAL")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN sl_trailed INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE paper_orders ADD COLUMN intraday_only INTEGER DEFAULT 0")  # BRACKET/COVER: forced square-off before close
        log.info("Migrated paper_orders: added trade_source/source_engine/stop_price/trailing-stop/intraday_only columns (Phase 3).")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, symbol TEXT, strike INTEGER, direction TEXT,
            trade_source TEXT NOT NULL DEFAULT 'MANUAL', source_engine TEXT,
            order_type TEXT DEFAULT 'MARKET', limit_price REAL, stop_price REAL,
            entry_price REAL, target_price REAL, sl_price REAL, qty INTEGER DEFAULT 1,
            trailing_stop_enabled INTEGER DEFAULT 0, trailing_trigger_pct REAL,
            trailing_giveback_pct REAL, breakeven_trigger_pct REAL,
            peak_price REAL, sl_trailed INTEGER DEFAULT 0, intraday_only INTEGER DEFAULT 0,
            entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
            status TEXT DEFAULT 'OPEN', trader_note TEXT, wallet_linked INTEGER DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_user_status ON paper_orders(user_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_source ON paper_orders(trade_source, source_engine)")

    # Phase 3: per-user AI Auto-Trading opt-in (each subscriber independently
    # enables/disables auto-trading for their OWN wallet, per engine -- applies
    # across every symbol that engine already trades; see fanout_auto_trade_entry).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_auto_trading_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, engine TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(user_id, engine)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auto_settings_engine_enabled ON user_auto_trading_settings(engine, enabled)")

    # Backtest parameter profiles -- a named, tuned parameter set per
    # (symbol, engine), created from the /backtest page. params_json is a
    # JSON blob (not explicit columns) since each engine's tunable-parameter
    # shape is independent and grows over time -- see ENGINE_PARAM_SPECS
    # below for the per-engine schema/defaults/validation. is_active_live is
    # only meaningful for engine='sr' (the only engine with a live auto-trade
    # path today, see get_sr_live_params) -- at most one row can have
    # is_active_live=1 per (symbol, engine='sr'), enforced transactionally in
    # the /backtest/profile/activate route, not by a DB constraint.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            engine TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            params_json TEXT NOT NULL,
            last_backtest_summary_json TEXT,
            is_active_live INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            activated_at TEXT, activated_by_user_id INTEGER,
            UNIQUE(symbol, engine, profile_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backtest_profiles_symbol_engine ON backtest_profiles(symbol, engine)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backtest_profiles_active_live ON backtest_profiles(engine, symbol, is_active_live)")

    # NOTE: `paper_trades` (below) is the Swing engine's own system-wide,
    # symbol-scoped reference-trade tracking (powers /calibration and
    # /signal-history) -- it is UNRELATED to, and deliberately untouched by,
    # the per-user `paper_orders` table above. One character apart; do not
    # confuse the two.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strike INTEGER, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL, confidence INTEGER,
            entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
            status TEXT DEFAULT 'OPEN'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scalp_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strike INTEGER, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL,
            entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
            status TEXT DEFAULT 'OPEN',
            risk_reward REAL, delta_used REAL, regime_multiplier REAL, volume_ratio REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scalp_paper_trades_symbol_status ON scalp_paper_trades(symbol, status)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS v3_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strike INTEGER, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL,
            entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
            status TEXT DEFAULT 'OPEN',
            risk_reward REAL, confidence INTEGER, regime_at_entry TEXT, prev_day_validation TEXT,
            factors_json TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_v3_paper_trades_symbol_status ON v3_paper_trades(symbol, status)")
    # Additive migration for DBs created before factors_json existed (feeds
    # learn_adaptive_weights -- see sr_engine_v3.py) -- idempotent, same
    # guarded-ALTER pattern used for paper_trades' institutional_score etc.
    v3_existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(v3_paper_trades)")}
    if "factors_json" not in v3_existing_cols:
        conn.execute("ALTER TABLE v3_paper_trades ADD COLUMN factors_json TEXT")
        log.info("Migrated v3_paper_trades: added column 'factors_json'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_structure_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, date TEXT, time TEXT, ts TEXT,
            atr_14 REAL, adx REAL, regime TEXT,
            pdh REAL, pdl REAL, pdc REAL, vwap REAL,
            swing_high REAL, swing_low REAL,
            mother_candle_json TEXT, liquidity_sweep_json TEXT, custom_levels_json TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_snapshot_symbol_date ON market_structure_snapshots(symbol, date, time)")
    for migration_col in ("swing_high REAL", "swing_low REAL"):
        try:
            conn.execute(f"ALTER TABLE market_structure_snapshots ADD COLUMN {migration_col}")
        except sqlite3.OperationalError:
            pass   # column already exists -- fine, this migration is idempotent

    # Data migration: save_market_structure_snapshot() used to store `ts` as a
    # raw epoch float (now.timestamp()) instead of an ISO string, unlike every
    # other `ts` column in this DB (e.g. cycles.ts). backtest.py's
    # load_market_structure_snapshots() always expected an ISO string (it calls
    # dt.datetime.fromisoformat(day_snapshot["ts"])) -- the mismatch crashed
    # every S/R-engine backtest touching a date with a saved snapshot, with
    # "fromisoformat: argument must be str". Fixed going forward in
    # save_market_structure_snapshot(); this converts existing rows in place
    # (SQLite's flexible typing accepts a TEXT value in this REAL-declared
    # legacy column fine). Idempotent: an already-migrated row's `ts` is no
    # longer a bare float, so int()/float() on it raises and the row is
    # skipped on subsequent runs.
    _bad_ts_rows = conn.execute("SELECT id, ts FROM market_structure_snapshots").fetchall()
    _migrated_ts_count = 0
    for _row_id, _ts_val in _bad_ts_rows:
        try:
            _epoch = float(_ts_val)
        except (TypeError, ValueError):
            continue   # already an ISO string (or NULL) -- nothing to do
        conn.execute(
            "UPDATE market_structure_snapshots SET ts=? WHERE id=?",
            (dt.datetime.fromtimestamp(_epoch).isoformat(), _row_id),
        )
        _migrated_ts_count += 1
    if _migrated_ts_count:
        log.info(f"Migrated market_structure_snapshots: converted {_migrated_ts_count} epoch-float "
                  f"'ts' value(s) to ISO strings (fixes backtest 'fromisoformat: argument must be str').")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol_status ON paper_trades(symbol, status)")

    # Safe migration: add calibration-tracking columns if they don't already exist
    # (lets us later check whether the Institutional Entry Score genuinely predicts
    # win-rate, without needing any ML -- just honest bucketed statistics).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
    for col, coltype in [("institutional_score", "INTEGER"), ("institutional_tier", "TEXT"),
                          ("regime_at_entry", "TEXT"), ("sr_level", "TEXT"), ("risk_reward", "REAL")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}")
            log.info(f"Migrated paper_trades: added column '{col}'")

    conn.commit()
    conn.close()
    log.info(f"Historical DB ready at {DB_PATH}")

    # Milestone 12, Phase 2A follow-up: agents/sys_admin's and agents/
    # runtime's own tables (agent_audit_log, agent_status, agent_events,
    # sysadmin_log, runtime_policy, runtime_workflow, ...) were NEVER
    # created against the live database -- confirmed by a read-only
    # investigation of production oi_history.db, which has 21 tables and
    # none of these. Every affected module's init_db() is CREATE TABLE
    # IF NOT EXISTS only (idempotent, additive-only -- no ALTER/DROP on
    # any existing table, no data migration), so calling them here is
    # the same "safe to run every startup" contract every other
    # migration in this function already follows. This only lets the
    # EXISTING read-only reporting surfaces (/api/sysadmin/overview,
    # /api/runtime/status) show real data instead of degrading to
    # "unavailable" -- it does not start the scheduler
    # (RUNTIME_SCHEDULER_ENABLED is untouched), does not change
    # RUNTIME_CONTROL_API_ENABLED, and does not make trading_intelligence/
    # quant_researcher schedulable (agents.runtime.scheduling_control.
    # NEVER_SCHEDULABLE_AGENTS is a code-level constant, unaffected by
    # which tables exist).
    agent_audit_log.init_db()
    agent_event_bus.init_db()
    agent_risk_store.init_db()
    agent_supervision_store.init_db()
    agent_sysadmin_store.init_db()
    agent_runtime_store.init_db()
    log.info("Runtime/sys_admin observability tables ready (agent_audit_log, agent_status, agent_events, "
             "sysadmin_log, runtime_policy, runtime_workflow, ...).")

    # Milestone 12, Phase 2B: Shadow Mode's own isolated table namespace
    # (shadow_observations, shadow_predictions, shadow_outcomes) --
    # CREATE TABLE IF NOT EXISTS only, same additive-only contract as
    # the six calls above. Creating these tables does NOT start
    # anything -- Shadow Mode has no background thread and no scheduler
    # wiring; observer.observe_and_predict() is only ever invoked
    # manually or by a test/future API action.
    shadow_store.init_db()
    log.info("Shadow Mode tables ready (shadow_observations, shadow_predictions, shadow_outcomes) -- "
             "read-only pipeline, no automatic execution.")

    # Hotfix: agents.trading_intelligence.ti_store.init_db() (creates
    # ti_paper_trades, the Milestone 10 AI-signal paper-trade table
    # ai_trading_engine.evaluate() reads via ti_store.list_open_trades())
    # was never actually called from production startup -- only from
    # test fixtures. CREATE TABLE IF NOT EXISTS only, same additive-only
    # contract as every call in this block; does not change any trading
    # logic, risk-manager behavior, or broker connectivity. Restores
    # /api/trading-intelligence/overview (and therefore the whole
    # trading-intelligence dashboard) from a 500 to working.
    ti_store.init_db()
    log.info("Trading Intelligence paper-trade table ready (ti_paper_trades).")

    # Milestone 13, Phase 2: Intelligence History's own isolated table
    # namespace (intelligence_snapshots_log) -- CREATE TABLE IF NOT
    # EXISTS only, same additive-only contract as every call above.
    # Creating this table does NOT start anything -- no background
    # thread, no scheduler wiring; a snapshot is only ever logged
    # manually via intelligence_history_cli.py.
    intelligence_history_store.init_db()
    log.info("Intelligence History table ready (intelligence_snapshots_log) -- "
             "read-only pipeline, no automatic execution.")

    # Milestone 14, Phase 1: Intelligence Alerts' own isolated table
    # namespace (intelligence_alerts_log) -- CREATE TABLE IF NOT EXISTS
    # only, same additive-only contract as every call above. Creating
    # this table does NOT start anything -- no background thread, no
    # scheduler wiring; a rule is only ever evaluated manually via
    # intelligence_alerts_cli.py.
    intelligence_alerts_store.init_db()
    log.info("Intelligence Alerts table ready (intelligence_alerts_log) -- "
             "read-only pipeline, no automatic execution.")

    # Milestone 14, Phase 3: threshold override table (intelligence_
    # alert_thresholds) -- CREATE TABLE IF NOT EXISTS only. Empty on a
    # fresh install; every threshold falls back to its agents/config.py
    # default until an admin explicitly sets an override via POST
    # /api/intelligence/alerts/config (off by default,
    # INTELLIGENCE_ALERT_CONFIG_API_ENABLED).
    intelligence_alerts_threshold_store.init_db()
    log.info("Intelligence Alert threshold override table ready (intelligence_alert_thresholds).")

    # Milestone 16, Phase 1: Persistent Runtime Event Log (ops_event_log)
    # -- CREATE TABLE IF NOT EXISTS only. Initialized early/here so every
    # other module below that calls ops_event_log.record_event_safe()
    # (dedup_store, rate_limiter, retry_tracker) has the table ready.
    ops_event_log.init_db()
    log.info("Ops event log table ready (ops_event_log).")

    # Milestone 15, Phase 1: Alert Deduplication & Cooldown Protection
    # state (intelligence_alert_dedup_state) -- CREATE TABLE IF NOT
    # EXISTS only. Empty on a fresh install; the very first evaluation
    # of any (symbol, bias, rule) condition always proceeds.
    intelligence_alerts_dedup_store.init_db()
    log.info("Intelligence Alert dedup state table ready (intelligence_alert_dedup_state).")

    # Milestone 15, Phase 2: Alert Rate Limiting & Retry Protection
    # tables (intelligence_alert_send_log, intelligence_alert_retry_state)
    # -- CREATE TABLE IF NOT EXISTS only.
    intelligence_alerts_rate_limiter.init_db()
    intelligence_alerts_retry_tracker.init_db()
    log.info("Intelligence Alert rate-limit and retry-tracker tables ready.")

    # Milestone 20, Phase 6: in-process 1m/3m/5m candle recorder's
    # write-through table (live_candles) -- CREATE TABLE IF NOT EXISTS
    # only. Populated by run_symbol_loop()'s own candle_recorder.
    # append_tick() call each cycle, zero new broker calls.
    candle_recorder.init_db()
    log.info("Live candle recorder table ready (live_candles).")

    # Milestone 20, Phase 7: adaptive structure-tuning audit log
    # (structure_tuning_log) -- CREATE TABLE IF NOT EXISTS only. Every
    # evaluation the bounded/rate-limited tuning pass runs (wired into
    # the TI cycle, see agent_runtime.py) is recorded here, applied or
    # not, with the full backtest evidence behind the decision.
    structure_tuning.init_db()
    log.info("Structure tuning audit log table ready (structure_tuning_log).")

    # Milestone 21, Phase 1: Virtual Trailing Engine's own state table
    # (virtual_trailing_state) -- CREATE TABLE IF NOT EXISTS only. A
    # paper-trade / advisory-only shadow layer over ti_paper_trades,
    # populated by the TI cycle when config.TI_ENABLE_VIRTUAL_TRAILING
    # is set (see agent_runtime.py); never touches a broker.
    virtual_trailing.init_db()
    log.info("Virtual trailing engine state table ready (virtual_trailing_state).")


def log_cycle_to_db(symbol, now, underlying, atm, pcr, max_pain, bias, note, signal, rows):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, max_pain, bias, note,
               signal_action, signal_strike, signal_direction, signal_entry, signal_target, signal_sl,
               signal_confidence, signal_tradeable)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, now.isoformat(), now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
             underlying, atm, pcr, max_pain, bias, note,
             signal.get("action"), signal.get("strike"), signal.get("direction"),
             signal.get("entry_price"), signal.get("target_price"), signal.get("sl_price"),
             signal.get("confidence"), int(bool(signal.get("tradeable")))),
        )
        cycle_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO strikes (cycle_id, strike, ce_oi, ce_oi_chg, ce_vol, ce_ltp, ce_chg_pct, ce_signal, ce_iv,
                                     ce_delta, ce_gamma, ce_theta, ce_vega,
                                     pe_oi, pe_oi_chg, pe_vol, pe_ltp, pe_chg_pct, pe_signal, pe_iv,
                                     pe_delta, pe_gamma, pe_theta, pe_vega)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(cycle_id, r.strike, r.ce_oi, r.ce_oi_chg, r.ce_vol, r.ce_ltp, r.ce_chg_pct, r.ce_signal, r.ce_iv,
              r.ce_delta, r.ce_gamma, r.ce_theta, r.ce_vega,
              r.pe_oi, r.pe_oi_chg, r.pe_vol, r.pe_ltp, r.pe_chg_pct, r.pe_signal, r.pe_iv,
              r.pe_delta, r.pe_gamma, r.pe_theta, r.pe_vega) for r in rows],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"DB log failed: {e}")


def send_telegram(msg: str) -> bool:
    """Milestone 15, Phase 2: now returns True only if a send was
    ATTEMPTED and didn't raise (matching intelligence_alerts_cli.py's
    own _send_telegram()'s exact semantics -- not a delivery
    confirmation, Telegram's response body is never checked). Purely
    additive: every existing call site already ignored the return value
    (fire-and-forget), so this changes nothing for them -- only the new
    retry_tracker.py wiring in _run_intelligence_alerts_auto_cycle()
    actually reads it."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5,
        )
        return True
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def send_telegram_channel(msg: str) -> bool:
    """Milestone 18: same fire-and-forget contract as send_telegram()
    above, but posts to TELEGRAM_SIGNALS_CHANNEL_ID (the public "IDaddy
    Scalping Signals" channel) instead of the personal admin chat.
    No-ops (returns False) until an admin sets TELEGRAM_SIGNALS_CHANNEL_ID
    -- same safe-by-default shape as every other optional integration in
    this file."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_SIGNALS_CHANNEL_ID):
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_SIGNALS_CHANNEL_ID, "text": msg}, timeout=5,
        )
        return True
    except Exception as e:
        log.warning(f"Telegram channel send failed: {e}")
        return False


def format_signal_open_message(symbol: str, sr_trigger: dict) -> str:
    """Milestone 18: the channel's entry-signal post. CE side is labeled
    BUY, PE side SELL -- matches how retail option-scalping channels
    conventionally describe market direction (bullish/bearish), NOT the
    literal order side -- this engine only ever buys option premium either
    way (see paper_trading.py's own module docstring), hence "Buy @"
    stays the same label on both sides below. Target 2/3 lines are
    omitted entirely -- not shown as "TBD" -- when
    three_targets_achievable is False, exactly as requested: show 3 or
    show 1, never a partial 2."""
    direction = sr_trigger["direction"]
    label = "BUY SIGNAL" if direction == "CE" else "SELL SIGNAL"
    emoji = "\U0001F7E2" if direction == "CE" else "\U0001F534"
    lines = [
        f"{emoji} {label} — {symbol} {sr_trigger['strike']} {direction}",
        "",
        f"Buy @ {sr_trigger['entry_price']}",
        f"Target 1 @ {sr_trigger['target1']}",
    ]
    if sr_trigger.get("three_targets_achievable"):
        lines.append(f"Target 2 @ {sr_trigger['target2']}")
        lines.append(f"Target 3 @ {sr_trigger['target3']}")
    lines.append(f"SL @ {sr_trigger['sl_price']}")
    return "\n".join(lines)


def format_signal_progress_message(symbol: str, open_trade: dict) -> str:
    """Milestone 18: periodic "still open" update for the channel --
    current LTP and % of the way to Target 1 (the actual exit level;
    Target 2/3 shown at entry are informational upside only, see
    get_sr_trade_trigger()'s own comment). Caller throttles how often
    this actually gets sent (see _telegram_progress_bucket below) --
    this function itself has no rate-limiting of its own."""
    entry, target, current = open_trade["entry_price"], open_trade["target_price"], open_trade["current_price"]
    target_distance = target - entry
    progress_pct = max(0, min(100, round((current - entry) / target_distance * 100))) if target_distance > 0 else 0
    return (f"\U0001F4CA {symbol} {open_trade['strike']}{open_trade['direction']} update: "
            f"LTP {current} ({progress_pct}% to Target 1 @ {target})")


def format_signal_close_message(symbol: str, open_trade: dict, exit_reason: str, points: float) -> str:
    """Milestone 18: the channel's exit post -- exit price/reason plus
    the P&L in real terms (points x lot qty), same "lot size x exit"
    calculation the existing close_msg/socketio alert already does
    (PAPER_TRADE_LOT_QTY), just also mirrored to the public channel."""
    emoji = "✅" if exit_reason == "TARGET HIT" else ("\U0001F534" if exit_reason == "STOP LOSS" else "⏱")
    pnl = round(points * PAPER_TRADE_LOT_QTY, 2)
    return (f"{emoji} EXIT — {symbol} {open_trade['strike']}{open_trade['direction']}\n"
            f"Exit @ {open_trade['exit_price']} ({exit_reason})\n"
            f"Points: {points:+.2f} | Lot Qty: {PAPER_TRADE_LOT_QTY} | P&L: ₹{pnl:+.2f}")


def _run_intelligence_alerts_auto_cycle(symbol):
    """Milestone 14, Phase 2: automatic intelligence snapshot logging +
    alert evaluation for one symbol's live cycle. Only ever called when
    agents_config.INTELLIGENCE_ALERTS_AUTO_ENABLED is explicitly true
    (off by default -- see agents/config.py's own comment) and only from
    run_symbol_loop()'s `if open_now:` branch (never on stale/dev-mode
    data). The ONLY caller wraps this in try/except, so a failure here
    can never affect paper trading, signal generation, or any other
    engine in the symbol loop.

    Still strictly read-only/non-trading: this can only ever INSERT one
    intelligence_snapshots_log row and one intelligence_alerts_log row,
    and send a Telegram/email NOTICE -- it never opens, closes, modifies,
    or queues a trade of any kind, and never calls a broker (build_snapshot()
    only reads already-fetched/already-stored data, same guarantee as the
    manual /api/intelligence/snapshot route)."""
    now = dt.datetime.now()

    # Milestone 15, Phase 2: Delivery Retry Protection -- resend any of
    # THIS symbol's own previously-failed deliveries whose backoff has
    # elapsed. Deliberately runs even when a fresh snapshot isn't
    # available this cycle (checked next) -- a retry redelivers an
    # OLD, already-decided message, so it must not depend on a new
    # trigger existing. Also deliberately bypasses dedup_store (that
    # decision was already made when the original send failed) but
    # still respects the rate limiter (a retry is still a real send
    # attempt).
    for pending in intelligence_alerts_retry_tracker.get_due_retries(now=now):
        if not pending["identity"].startswith(f"{symbol}|"):
            continue
        if not intelligence_alerts_rate_limiter.is_allowed(
            symbol=symbol,
            max_per_symbol_per_hour=agents_config.INTELLIGENCE_ALERT_MAX_PER_SYMBOL_PER_HOUR,
            max_total_per_hour=agents_config.INTELLIGENCE_ALERT_MAX_TOTAL_PER_HOUR,
            now=now,
        ):
            continue  # still rate-limited -- try again a later cycle
        if send_telegram(pending["message"]):
            intelligence_alerts_retry_tracker.record_success(pending["identity"])
            intelligence_alerts_rate_limiter.record_send(symbol, now=now)
            ops_event_log.record_event_safe(
                ops_models.ALERT_SENT, {"identity": pending["identity"], "symbol": symbol, "via": "retry"}, now=now,
            )
        else:
            intelligence_alerts_retry_tracker.record_failure(pending["identity"], pending["message"], now=now)

    snapshot = intelligence_orchestrator.build_snapshot(symbol)
    if snapshot is None:
        return

    intelligence_history_store.record_snapshot(
        ts=now.isoformat(), symbol=symbol, timeframe=intelligence_orchestrator.DEFAULT_TIMEFRAME, snapshot=snapshot,
    )

    # Milestone 15, Phase 1: Alert Deduplication & Cooldown Protection --
    # replaces the old flat (symbol, rule)-keyed cooldown check with a
    # fingerprint of (symbol, bias, confidence bucket, rule). See
    # agents/intelligence_alerts/dedup_store.py's own docstring for the
    # bypass rules (bias change, rule change, confidence-bucket increase).
    dedup_cooldown = intelligence_alerts_threshold_store.get_effective_config()["dedup_cooldown_seconds"]
    for triggered in intelligence_alerts_rules.evaluate_all(symbol=symbol):
        suppressed = intelligence_alerts_dedup_store.should_suppress(
            symbol=symbol, bias=snapshot.bias, confidence=snapshot.confidence, rule=triggered["rule"],
            cooldown_seconds=dedup_cooldown, now=now,
        )
        if suppressed:
            continue  # still in cooldown -- don't re-alert the same condition every cycle

        # Milestone 15, Phase 2: Alert Rate Limiting -- caps raw alert
        # VOLUME (distinct conditions) independent of dedup, which only
        # governs repeats of the SAME condition.
        if not intelligence_alerts_rate_limiter.is_allowed(
            symbol=symbol,
            max_per_symbol_per_hour=agents_config.INTELLIGENCE_ALERT_MAX_PER_SYMBOL_PER_HOUR,
            max_total_per_hour=agents_config.INTELLIGENCE_ALERT_MAX_TOTAL_PER_HOUR,
            now=now,
        ):
            continue  # rate-limited -- dedup_store already recorded this condition as "sent" above

        msg = f"[Intelligence Alert] {triggered['detail']}"
        identity = intelligence_alerts_cooldown.make_fingerprint(
            symbol=symbol, bias=snapshot.bias, confidence=snapshot.confidence, rule=triggered["rule"],
        )
        sent_ok = send_telegram(msg)
        if sent_ok:
            intelligence_alerts_retry_tracker.record_success(identity)
            intelligence_alerts_rate_limiter.record_send(symbol, now=now)
            ops_event_log.record_event_safe(
                ops_models.ALERT_SENT, {"identity": identity, "symbol": symbol, "rule": triggered["rule"], "via": "fresh"},
                now=now,
            )
        else:
            intelligence_alerts_retry_tracker.record_failure(identity, msg, now=now)
        delivered_telegram = sent_ok
        delivered_email = False
        if agents_config.INTELLIGENCE_ALERT_EMAIL_TO:
            delivered_email = auth.send_email(agents_config.INTELLIGENCE_ALERT_EMAIL_TO, f"Intelligence Alert: {triggered['rule']}", msg)
        intelligence_alerts_store.record_alert(
            ts=now.isoformat(), symbol=symbol, rule=triggered["rule"], detail=triggered["detail"],
            delivered_telegram=delivered_telegram, delivered_email=delivered_email,
        )


# ----------------------------------------------------------------------------
# BACKGROUND LOOP -- reads state["symbol_viewers"] every cycle so switching
# from the browser takes effect on the very next tick.
# ----------------------------------------------------------------------------

state = {
    # Per-symbol viewer counts (symbol -> set of socket sids currently
    # watching it), NOT a single global "current_symbol" -- a single shared
    # value meant two browser tabs/users on different symbols would
    # constantly steal each other's fast-refresh slot and broadcast stream.
    # A symbol gets full REFRESH_INTERVAL speed whenever ITS OWN viewer set
    # is non-empty, independent of what any other tab/user is looking at.
    "symbol_viewers": {symbol: set() for symbol in SYMBOLS},
    "sid_symbol": {},   # sid -> symbol currently joined, for switch/disconnect cleanup
    "history_by_symbol": {},
    "alerts_by_symbol": {},
    "last_bias_by_symbol": {},
    "prev_token_state_by_symbol": {},
    "paper_by_symbol": {},
    "scalp_paper_by_symbol": {},          # separate paper-trade bucket for the Scalping Engine -- own win-rate, never mixed with the S/R engine's
    "scalp_cooldown_until_by_symbol": {},  # {symbol: datetime or None} -- pause scalp re-entry after a scalp stop-loss (independent of the S/R engine's cooldown)
    "bias_streak_by_symbol": {},      # {symbol: {"bias": str, "count": int}} -- persistence filter
    "cooldown_until_by_symbol": {},   # {symbol: datetime or None} -- pause after a stop-loss
    "consecutive_time_exits_by_symbol": {},   # {symbol: int} -- tracks non-resolving trades in a row
    "commentary_by_symbol": {},   # {symbol: {"text":..., "last_refresh": datetime}}
    "commentary_seen_symbols": set(),   # for the ChatGPT module's one-time-disclaimer logic
    "ollama_insight_by_symbol": {},   # {symbol: {"insight": dict, "last_refresh": datetime}}
    "backtest_job": {"running": False, "done": 0, "total": 0, "elapsed": 0, "trades_so_far": 0, "result": None, "form": None, "error": None, "started_at": None, "token": 0},
    "ollama_request_in_flight": set(),   # symbols currently being processed -- prevents pile-up
    "sr_state_by_symbol": {},   # {symbol: {level_key: state_dict}} -- the WATCH->ARMED->CONFIRMED->ACTIVE->... state machine
    "dev_settings": {
        "DEV_MODE_WHEN_CLOSED": DEV_MODE_WHEN_CLOSED,
        "PAPER_TRADING_ENABLED": PAPER_TRADING_ENABLED,
        "TRAILING_SL_ENABLED": TRAILING_SL_ENABLED,
        "STAGNANT_EXIT_ENABLED": STAGNANT_EXIT_ENABLED,
        "SIGNAL_CONFIDENCE_THRESHOLD": SIGNAL_CONFIDENCE_THRESHOLD,
        "VOLUME_EXPANSION_MULT": VOLUME_EXPANSION_MULT,
    },  # toggle these live from /dev-settings -- no .env edit or restart needed
    "nse_status": "Not yet attempted",
    "market_status": None,   # {"symbol":..., "open":..., "reason":..., "checked_at":...} -- last known, for new connects
    "market_status_by_symbol": {},   # every symbol's own status, regardless of which is being viewed
    "market_structure_by_symbol": {},   # {symbol: {..structure levels.., "computed_date": date}}
    "dynamic_sr_by_symbol": {},   # {symbol: dynamic_sr_engine.evaluate() output} -- new PDH/PDL-based S/R engine (V1, pure price/volume, runs alongside the OI-based engines)
    "recent_candles_by_symbol": {},     # {symbol: [candle dicts]} -- for 3-candle-close signal confirmation
    "engine_v2_enabled": {"NATURALGAS": True, "NIFTY": True},   # {symbol: bool} -- NATURALGAS/NIFTY default ON per request; others opt-in via /dev-settings
    "new_trend_meter_enabled": True,   # global toggle -- ON by default since 2026-08-04 (now fuses Ichimoku as a regime-weighted trend-confirmation factor, see oi_engine.compute_new_trend_meter); still fully opt-out via /dev-settings. DISPLAY/ADVISORY ONLY -- see compute_trend_meter's "does not feed back into trading decisions" note, unchanged.
    "ichimoku_engine_enabled": True,   # global toggle -- ON by default per request; kill-switch only, wrapped in try/except so a failure here can never break any other engine. ADVISORY/DISPLAY ONLY -- does not gate fanout_auto_trade_entry / real order placement (see ichimoku_engine.py's module docstring).
    "ichimoku_candles_by_symbol": {},   # {symbol: [candle dicts]} -- FULL history (not the 30-candle recent_candles_by_symbol slice), refreshed alongside market_structure every ~2min, reused (no extra API calls)
    "ichimoku_signal_by_symbol": {},    # {symbol: ichimoku_engine.analyze() output} -- latest evaluation
    "ichimoku_paper_by_symbol": {},     # own separate paper-trade bucket (underlying points, not premium) -- logs recommendation+outcome so accuracy can be measured before this is ever trusted with real execution
    "scalp_engine_enabled": False,   # global toggle -- OFF by default (new, un-backtested strategy; ADVISORY ONLY, never opens paper/real trades on its own), opt-in via /dev-settings
    "scalp_premium_history": {},   # {symbol: {(strike,direction): [{"ltp","time"}, ...]}} -- in-memory only, prior-readings-only per evaluate_scalp_candidate's contract
    "scalp_volume_history": {},    # {symbol: {(strike,direction): [volume, ...]}}
    "scalp_signal_by_symbol": {},  # {symbol: {"CE": {...}, "PE": {...}}} -- latest scalp_engine evaluation
    "manual_trade_delete_enabled": False,   # OFF by default -- gates delete-buttons on the manual-trading page
    "v2_by_symbol": {},        # {symbol: build_v2_probability_table() output} -- read-only display, no trading impact
    "last_payload_by_symbol": {},   # {symbol: last emitted 'update' payload} -- so new connects don't wait a full cycle
    "auto_fanout_cooldown": {},   # {(user_id, engine, symbol): datetime} -- per-user AI Auto-Trading re-entry cooldown after an SL exit; separate from cooldown_until_by_symbol/scalp_cooldown_until_by_symbol (those gate the unchanged system-wide reference trade, this gates fan-out only)

    # -- S/R Engine V3 (dynamic S/R + OI-cluster + Greeks + prev-day validation/extension) --
    # OFF by default per-symbol -- brand-new/un-backtested, same opt-in posture as the Scalping Engine.
    "v3_engine_enabled": {},
    "v3_signal_by_symbol": {},       # {symbol: generate_v3_signal() output} -- latest evaluation
    "v3_paper_by_symbol": {},        # separate paper-trade bucket -- own win-rate, never mixed with V1/V2/Scalp
    "v3_cooldown_until_by_symbol": {},   # {symbol: datetime or None} -- pause re-entry after a V3 stop-loss
    "v3_today_ltp_by_symbol": {},    # {symbol: {"date": "YYYY-MM-DD", "ltps": deque}} -- resets at day boundary, for Held/Broke/Flipped classification
    "v3_volume_history_by_symbol": {},   # {symbol: {(strike,"CE"/"PE"): deque}} -- rolling volume for extension-detection's volume-expansion check
    "v3_prev_day_validation_by_symbol": {},   # {symbol: {"date": "YYYY-MM-DD", "result": validate_previous_day_levels() output}} -- computed once per day, cached
    "v3_adaptive_weights_by_symbol": {},   # {symbol: {"date":..., "weights":..., "diagnostics":...}} -- learn_adaptive_weights(), recomputed once per day
    "v3_wall_center_history_by_symbol": {},   # {symbol: {"support": deque, "resistance": deque}} -- prior OI-cluster-center readings for detect_wall_migration
}


SR_LEVEL_TYPE = {"resistance": "resistance", "resistance_reversal": "resistance",
                  "support": "support", "support_reversal": "support"}
SR_ACTIVE_RESET_MINUTES = int(os.getenv("SR_ACTIVE_RESET_MINUTES", "20"))   # after TARGET_HIT/STOPPED, allow a fresh setup
SR_MIN_RISK_REWARD = float(os.getenv("SR_MIN_RISK_REWARD", "1.5"))   # minimum R:R for the S/R engine's own setups
STAGED_TARGET_MODE = os.getenv("STAGED_TARGET_MODE", "conservative")   # "conservative" (Min of T3/structural-level) or "optimistic" (structural-level) -- both computed always, this just picks which shows as "final" -- compare via backtest before switching
PREMIUM_EMA_FAST = int(os.getenv("PREMIUM_EMA_FAST", "5"))    # fast EMA period for premium momentum confirmation
PREMIUM_EMA_SLOW = int(os.getenv("PREMIUM_EMA_SLOW", "10"))   # slow EMA period -- fast>slow required before entry triggers
SR_PROXIMITY_ATR_MULT = float(os.getenv("SR_PROXIMITY_ATR_MULT", "3.0"))   # CONFIRMED requires price within this many ATRs of the level

# Single source of truth for every tunable parameter the /backtest page can
# expose, per engine: default value (today's exact global/module constant --
# NOT a new tuned value), type, and a [min, max] sanity range. Used for (a)
# server-side validation on /backtest/profile/save, (b) the client-side
# "Reset to Defaults" button (serialized into the page as JSON, no server
# round-trip needed), (c) safe fallback when a saved profile predates a param
# added later (missing key -> this dict's default, never a KeyError).
# NOTE: confidence_threshold lives under "old" only -- the live "sr" trigger
# (get_sr_trade_trigger) hardcodes a nominal confidence of 75 once the state
# machine + R:R gates already pass; SIGNAL_CONFIDENCE_THRESHOLD does not
# gate it. "v2" is intentionally absent -- it's read-only/advisory, no
# trading parameters exist to tune.
ENGINE_PARAM_SPECS = {
    "sr": {
        "min_risk_reward":               {"type": float, "default": SR_MIN_RISK_REWARD, "min": 0.1, "max": 20},
        "proximity_atr_mult":            {"type": float, "default": SR_PROXIMITY_ATR_MULT, "min": 0.1, "max": 10},
        "premium_ema_fast":              {"type": int,   "default": PREMIUM_EMA_FAST, "min": 1, "max": 50},
        "premium_ema_slow":              {"type": int,   "default": PREMIUM_EMA_SLOW, "min": 2, "max": 100},
        "target_delta_approx":           {"type": float, "default": TARGET_DELTA_APPROX, "min": 0.05, "max": 1.0},
        "min_target_pct":                {"type": float, "default": MIN_TARGET_PERCENT, "min": 0.01, "max": 1.0},
        "max_sl_pct":                    {"type": float, "default": MAX_SL_PERCENT, "min": 0.01, "max": 1.0},
        "max_hold_minutes":              {"type": int,   "default": MAX_HOLD_MINUTES, "min": 1, "max": 240},
        "breakeven_trigger_pct":         {"type": float, "default": BREAKEVEN_TRIGGER_PCT, "min": 0, "max": 1},
        "trail_trigger_pct":             {"type": float, "default": TRAIL_TRIGGER_PCT, "min": 0, "max": 1},
        "trail_giveback_pct":            {"type": float, "default": TRAIL_GIVEBACK_PCT, "min": 0, "max": 1},
        "cooldown_minutes_after_sl":     {"type": int,   "default": COOLDOWN_MINUTES_AFTER_SL, "min": 0, "max": 240},
        "cooldown_minutes_after_timeout": {"type": int,  "default": COOLDOWN_MINUTES_AFTER_TIMEOUT, "min": 0, "max": 240},
        # backtest-only knob (no live equivalent -- live has no such throttle) -- see simulate_sr_engine_trades's cooldown_minutes
        "sr_cooldown_minutes":           {"type": int,   "default": 0, "min": 0, "max": 240, "backtest_only": True},
    },
    "old": {
        "persistence_cycles":   {"type": int, "default": 5, "min": 1, "max": 50},
        "cooldown_minutes":     {"type": int, "default": COOLDOWN_MINUTES_AFTER_SL, "min": 0, "max": 240},
        "confidence_threshold": {"type": int, "default": SIGNAL_CONFIDENCE_THRESHOLD, "min": 0, "max": 100},
    },
    "v3": {
        "min_risk_reward":           {"type": float, "default": V3_MIN_RISK_REWARD, "min": 0.1, "max": 20},
        "confidence_threshold":      {"type": float, "default": V3_CONFIDENCE_TRADE_THRESHOLD, "min": 0, "max": 100},
        "min_target_pct":            {"type": float, "default": V3_MIN_TARGET_PCT, "min": 0.01, "max": 1.0},
        "max_sl_pct":                {"type": float, "default": V3_MAX_SL_PCT, "min": 0.01, "max": 1.0},
        "max_hold_minutes":          {"type": int,   "default": V3_MAX_HOLD_MINUTES, "min": 1, "max": 240},
        "cooldown_minutes_after_sl": {"type": int,   "default": V3_COOLDOWN_MINUTES_AFTER_SL, "min": 0, "max": 240},
        # V3_DEFAULT_FACTOR_WEIGHTS (a 7-key dict) intentionally excluded from
        # this UI -- it already has its own per-symbol adaptive-learning
        # mechanism (learn_adaptive_weights); a raw-JSON textarea for it is a
        # reasonable future addition, not required here.
    },
    "dynamic-sr": {   # V1 -- backtest-only, see plan: no live trading path exists for this engine today
        "min_tradeable_confidence": {"type": float, "default": MIN_TRADEABLE_CONFIDENCE, "min": 0, "max": 100},
        "max_hold_minutes":         {"type": int,   "default": MAX_HOLD_MINUTES, "min": 1, "max": 240},
    },
    "dynamic-sr-v4": {   # V4 -- backtest-only, see plan: no live trading path exists for this engine today
        "atr_trail_mult":             {"type": float, "default": exit_engine_v4.ATR_TRAIL_MULT, "min": 0.1, "max": 10},
        "momentum_fade_threshold":    {"type": float, "default": exit_engine_v4.MOMENTUM_FADE_THRESHOLD, "min": 0, "max": 100},
        "adaptive_hold_base_minutes": {"type": int,   "default": exit_engine_v4.ADAPTIVE_HOLD_BASE_MINUTES, "min": 1, "max": 240},
        "adaptive_hold_max_minutes":  {"type": int,   "default": exit_engine_v4.ADAPTIVE_HOLD_MAX_MINUTES, "min": 1, "max": 240},
        # max_sl_atr_mult: caps the entry SL's structural distance at N x
        # entry ATR (see exit_engine_v4.open_position) -- 1.5 is the
        # cross-symbol-validated recommendation (NIFTY/BANKNIFTY/SENSEX/
        # FINNIFTY 3-month sweep, 2026-08-05: reduces stop-loss severity
        # with IDENTICAL trade counts/win rates to uncapped on every symbol
        # tested; 1.25 looked better in isolation on NIFTY but introduced a
        # new whipsaw stop-out on BANKNIFTY, so it's not the default).
        # nullable=True: unlike every other knob here, "no cap" is a real,
        # meaningfully different mode (today's exact pre-feature behavior),
        # not just "the stock default value" -- the UI renders this one with
        # an enable/disable checkbox, and the disabled/blank state must
        # resolve to None (uncapped), not to some numeric default.
        "max_sl_atr_mult":            {"type": float, "default": 1.5, "min": 0.5, "max": 3.0, "step": 0.1,
                                        "nullable": True},
        # atr_target_mults (a 3-tuple) intentionally excluded -- same
        # "structured value, not a single scalar" reasoning as v3's factor
        # weights above; simple scalar overrides ship first.
    },
}


def get_sr_live_params(symbol):
    """Resolves the 'sr' engine's live-tunable params for `symbol`: the
    subscriber-activated backtest_profiles row (is_active_live=1) merged over
    ENGINE_PARAM_SPECS["sr"]'s defaults, or pure defaults if none is active.
    This is what makes /backtest/profile/activate consequential -- every live
    call site below (advance_sr_state_machine, update_paper_trading,
    update_paper_orders' AUTO-SWING cooldown) reads through here instead of
    the bare global constants, so a tuned profile takes effect for ALL
    subscribers auto-trading this symbol on the next live cycle.

    Cached in state["sr_active_profile_cache"] per symbol -- a DB read on
    every cycle for every symbol is unnecessary; the cache is invalidated
    (popped) by /backtest/profile/activate|deactivate|delete so a change
    takes effect starting the very next cycle, not on some polling delay."""
    cache = state.setdefault("sr_active_profile_cache", {})
    if symbol in cache:
        return cache[symbol]
    params = {k: meta["default"] for k, meta in ENGINE_PARAM_SPECS["sr"].items()
              if not meta.get("backtest_only")}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT params_json FROM backtest_profiles WHERE symbol=? AND engine='sr' AND is_active_live=1",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        params.update(json.loads(row["params_json"]))
    cache[symbol] = params
    return params


def get_sr_trade_trigger(symbol, market_structure=None):
    """
    Scans the (internal, non-serialized) S/R state-machine state for a level
    that just became ACTIVE + genuinely triggered (price broke its own recent
    premium high) with an acceptable Risk:Reward -- this is now the ONLY
    trigger that opens a paper trade (replaces the old oi_engine signal
    pathway). Marks the level as 'trade_opened' so it doesn't reopen every
    cycle while still ACTIVE+triggered.
    """
    bucket = state["sr_state_by_symbol"].get(symbol, {})
    for level_key, st in bucket.items():
        if (st.get("state") == "ACTIVE" and st.get("triggered") and not st.get("trade_opened")
                and st.get("risk_reward_ok")):
            st["trade_opened"] = True
            # Milestone 18 (Telegram scalping-signal channel): target1/target2
            # were already computed by compute_dynamic_targets_sl() but only
            # target1 was ever surfaced here (it's the ONLY level the actual
            # exit logic in update_paper_trading() checks -- see its own
            # "current_price >= open_trade['target_price']" check, unchanged).
            # target2/target3 below are informational only, for the channel
            # message's "further upside" display -- they do NOT affect where
            # the trade actually exits. target3 extrapolates one more leg of
            # target2's own spacing beyond target1 (same progressive spacing
            # the engine already established between target1 and target2,
            # not a new formula). "Achievable" (show 3 vs show 1 only) is
            # gated on target3 clearing the SAME min-risk-reward bar that
            # already gates target1 today.
            target1, target2, sl = st["target1"], st.get("target2"), st["sl"]
            entry_price = st["entry_price"]
            target3 = None
            three_targets_achievable = False
            if target2 is not None:
                target3 = round(target2 + (target2 - target1), 2)
                live_params = get_sr_live_params(symbol)
                _, three_targets_achievable = validate_risk_reward(
                    entry_price, target3, sl, min_rr=live_params["min_risk_reward"]
                )
            return {
                "strike": st["entry_strike"], "direction": st["direction_opt"],
                "entry_price": entry_price, "target_price": target1,
                "target1": target1, "target2": target2, "target3": target3,
                "three_targets_achievable": three_targets_achievable,
                "sl_price": sl, "confidence": 75,   # nominal -- this pathway already passed
                                                            # probability + state-machine + structural-trigger
                                                            # + momentum-confirmation + R:R gates
                "level_key": level_key,
                "institutional_score": st.get("institutional_score"),
                "institutional_tier": st.get("institutional_tier"),
                "risk_reward": st.get("risk_reward"),
                "regime_at_entry": (market_structure or {}).get("regime"),
            }
    return None


def advance_sr_state_machine(symbol, sr_table, underlying, market_structure, rows, atm, strike_step,
                              history=None, resistance_wall_confirmed=None, support_wall_confirmed=None):
    """Runs all 4 levels through the WATCH->ARMED->CONFIRMED->ACTIVE->TARGET_HIT/
    STOPPED lifecycle. Persists state in state['sr_state_by_symbol']. Returns a
    dict of level_key -> state info (including strike candidates once ACTIVE)
    for the dashboard."""
    if not sr_table:
        return None
    live_params = get_sr_live_params(symbol)
    atr = (market_structure or {}).get("atr_14")
    bucket = state["sr_state_by_symbol"].setdefault(symbol, {})
    now = dt.datetime.now()
    result = {}
    price_structure_hint = classify_price_structure(list(history)) if history else "INSUFFICIENT_DATA"

    for level_key, level_eval in sr_table.items():
        if level_key == "computed_at" or not isinstance(level_eval, dict):
            continue
        level_type = SR_LEVEL_TYPE[level_key]
        level_price = level_eval["value"]
        tolerance = atr * 0.3 if atr else max(1, level_price * 0.001)

        prev = bucket.get(level_key)
        st = advance_level_state(level_eval, prev, underlying=underlying, level_price=level_price,
                                  proximity_atr=atr, proximity_mult=live_params["proximity_atr_mult"])

        prev_state_str = (prev or {}).get("state")
        if st["state"] != prev_state_str:
            log.info(f"S/R STATE CHANGE | {symbol} {level_key} | {prev_state_str} -> {st['state']} "
                     f"| direction={st.get('direction')} | level_price={level_price} | underlying={underlying} "
                     f"| distance_label={st.get('distance_label')}")

        if st["state"] == "CONFIRMED":
            tick_triggered = check_structural_trigger(level_type, st["direction"], level_price, underlying, tolerance)
            candle_confirmed = check_candle_close_confirmation(
                level_type, st["direction"], level_price, state["recent_candles_by_symbol"].get(symbol, []), required_closes=3
            )
            triggered = tick_triggered and candle_confirmed
            if triggered:
                st = {**st, "state": "ACTIVE", "since": now, "level_price": level_price,
                      "level_type": level_type}
        elif st["state"] == "ACTIVE":
            next_level = None
            if level_type == "resistance" and st["direction"] == "breakout":
                next_level = level_price + (level_price - (sr_table.get("support") or {}).get("value", level_price)) * 0.25
            elif level_type == "support" and st["direction"] == "breakdown":
                next_level = level_price - ((sr_table.get("resistance") or {}).get("value", level_price) - level_price) * 0.25
            elif level_type == "resistance" and st["direction"] == "reversal":
                next_level = (sr_table.get("support") or {}).get("value", level_price)
            elif level_type == "support" and st["direction"] == "reversal":
                next_level = (sr_table.get("resistance") or {}).get("value", level_price)

            st = advance_active_level(st, underlying, st.get("level_price", level_price), next_level,
                                       st.get("level_type", level_type), st["direction"], tolerance)

            if st["state"] == "ACTIVE" and rows:
                direction_opt = "CE" if (st["direction"] in ("breakout",) or (st["direction"] == "reversal" and level_type == "support")) else "PE"
                candidates = score_strike_candidates(rows, atm, strike_step, direction_opt)
                st["strike_candidates"] = candidates[:3]
                st["direction_opt"] = direction_opt

                # -- Phase 3: dynamic entry trigger, then target/SL/R:R once triggered --
                if candidates and candidates[0]["liquidity_ok"]:
                    best = candidates[0]
                    premium_bucket = state.setdefault("sr_premium_history", {}).setdefault(symbol, {}).setdefault(level_key, [])
                    volume_bucket = state.setdefault("sr_volume_history", {}).setdefault(symbol, {}).setdefault(level_key, [])

                    if not st.get("triggered"):
                        # Compute the trigger from PRIOR readings only (before this cycle's
                        # reading is added) -- otherwise "break the recent high" becomes
                        # impossible to satisfy on the exact cycle a new high is set.
                        trigger = compute_premium_entry_trigger(list(premium_bucket))
                        st["entry_trigger"] = trigger
                        st["premium_momentum_pct"] = compute_premium_momentum(premium_bucket)
                        ema_confirmed, ema_fast, ema_slow = check_premium_momentum_confirmed(
                            list(premium_bucket), live_params["premium_ema_fast"], live_params["premium_ema_slow"]
                        )
                        st["premium_ema_fast"], st["premium_ema_slow"] = ema_fast, ema_slow
                        st["premium_ema_confirmed"] = ema_confirmed

                        # Fake Breakout Filter: volume expansion + OI-direction support +
                        # premium momentum + VWAP alignment, checked together as one gate.
                        # THE dominant entry-blocking gate (~98% of blocks, found 2026-07-21) --
                        # now live-tunable via /dev-settings (VOLUME_EXPANSION_MULT below) so
                        # signal-frequency can genuinely be adjusted without a restart. Lower =
                        # more signals pass (less strict); higher = fewer, stricter signals.
                        vol_expanded, vol_ratio = compute_volume_expansion(list(volume_bucket), best.get("volume"), expansion_mult=state["dev_settings"].get("VOLUME_EXPANSION_MULT", VOLUME_EXPANSION_MULT))
                        st["volume_expansion_ratio"] = vol_ratio
                        _prob_keys_local = [k for k in level_eval if k.endswith("_probability")]
                        _a_key_local, _b_key_local = _prob_keys_local[0], _prob_keys_local[1]
                        oi_supports = max(level_eval.get(_a_key_local, 0), level_eval.get(_b_key_local, 0)) >= 55
                        vwap_val = (market_structure or {}).get("vwap")
                        is_bullish_dir = st["direction"] == "breakout" or (st["direction"] == "reversal" and level_type == "support")
                        vwap_ok = (underlying > vwap_val) if (vwap_val and is_bullish_dir) else \
                                  (underlying < vwap_val) if vwap_val else None
                        filter_passes, filter_failed = fake_breakout_filter(
                            volume_expanded=vol_expanded, oi_supports_direction=oi_supports,
                            premium_rising=ema_confirmed, vwap_aligned=vwap_ok,
                        )
                        st["fake_breakout_filter_passed"] = filter_passes
                        st["fake_breakout_filter_reasons"] = filter_failed

                        # Smart-scalping gate: genuine breakout of the recent high AND premium
                        # EMA trend AND the Fake Breakout Filter (volume/OI/VWAP) must ALL agree.
                        if trigger is not None and best["ltp"] >= trigger and ema_confirmed and filter_passes:
                            st["triggered"] = True
                            st["entry_price"] = best["ltp"]
                            st["entry_strike"] = best["strike"]
                            atr = (market_structure or {}).get("atr_14")
                            swing_level = (market_structure or {}).get("swing_low") if direction_opt == "CE" else (market_structure or {}).get("swing_high")
                            t1, t2, sl = compute_dynamic_targets_sl(
                                best["ltp"], level_price, next_level, atr, delta_approx=live_params["target_delta_approx"],
                                min_target_pct=live_params["min_target_pct"], max_sl_pct=live_params["max_sl_pct"],
                                swing_level_underlying=swing_level, underlying_price=underlying,
                            )
                            st["target1"], st["target2"], st["sl"] = t1, t2, sl
                            rr, meets_min = validate_risk_reward(best["ltp"], t1, sl, min_rr=live_params["min_risk_reward"])
                            st["risk_reward"], st["risk_reward_ok"] = rr, meets_min

                            staged_direction = "bullish" if direction_opt == "CE" else "bearish"
                            st["staged_targets"] = compute_staged_underlying_targets(underlying, atr, next_level, direction=staged_direction, active_mode=STAGED_TARGET_MODE)

                            wall_confirmed_hint = resistance_wall_confirmed if level_type == "resistance" else support_wall_confirmed
                            prob_keys = [k for k in level_eval if k.endswith("_probability")]
                            a_key, b_key = prob_keys[0], prob_keys[1]
                            vwap = (market_structure or {}).get("vwap")
                            is_bullish_dir = st["direction"] == "breakout" or (st["direction"] == "reversal" and level_type == "support")
                            vwap_aligned = (underlying > vwap) if (vwap and is_bullish_dir) else \
                                           (underlying < vwap) if vwap else None
                            dominant_pct = max(level_eval.get(a_key, 50), level_eval.get(b_key, 50))
                            inst_score = compute_institutional_entry_score(
                                price_structure=price_structure_hint or "INSUFFICIENT_DATA",
                                oi_evidence_pct=dominant_pct, vwap_aligned=vwap_aligned,
                                regime=(market_structure or {}).get("regime"),
                                premium_momentum_confirmed=bool(ema_confirmed),
                                wall_cross_verified=bool(wall_confirmed_hint),
                                liquidity_score=best.get("score"), risk_reward_ok=meets_min,
                            )
                            st["institutional_score"] = inst_score["score"]
                            st["institutional_tier"] = inst_score["tier"]
                            st["institutional_breakdown"] = inst_score["breakdown"]
                        else:
                            # ACTIVE but entry blocked -- log WHICH gate, throttled to
                            # once per ~60s per level (avoids log-spam while preserving
                            # the diagnostic trail we were missing).
                            last_block_log = st.get("last_block_log_ts")
                            if not last_block_log or (now - last_block_log).total_seconds() >= 60:
                                st["last_block_log_ts"] = now
                                reasons = []
                                if trigger is None:
                                    reasons.append("no entry-trigger yet (insufficient premium history)")
                                elif best["ltp"] < trigger:
                                    reasons.append(f"premium {best['ltp']} below trigger {round(trigger, 2)}")
                                if not ema_confirmed:
                                    reasons.append(f"EMA momentum not aligned (fast={ema_fast}, slow={ema_slow})")
                                if not filter_passes:
                                    reasons.append(f"fake-breakout filter blocked ({'; '.join(filter_failed)})")
                                log.info(f"S/R ENTRY BLOCKED | {symbol} {level_key} | strike={best['strike']} "
                                         f"| {' AND '.join(reasons) if reasons else 'unknown'}")

                    premium_bucket.append({"ltp": best["ltp"], "time": now.strftime("%H:%M:%S")})
                    if len(premium_bucket) > 20:
                        del premium_bucket[:-20]
                    volume_bucket.append(best.get("volume"))
                    if len(volume_bucket) > 20:
                        del volume_bucket[:-20]

        elif st["state"] in ("TARGET_HIT", "STOPPED"):
            resolved_at = st.get("resolved_at")
            if not resolved_at:
                st["resolved_at"] = now
            elif (now - resolved_at).total_seconds() > SR_ACTIVE_RESET_MINUTES * 60:
                st = {"state": "NO_EDGE", "since": now, "direction": st["direction"],
                      "target_state": "NO_EDGE", "target_since": now, "grace_since": None}

        bucket[level_key] = st
        result[level_key] = _serialize_sr_state(st)

    return result


def _serialize_sr_state(st):
    """Datetime objects aren't JSON-serializable for socketio.emit -- convert
    to strings for the payload copy while keeping the original (with real
    datetime objects) in state['sr_state_by_symbol'] for internal use."""
    out = dict(st)
    for key in ("since", "target_since", "grace_since", "resolved_at", "last_block_log_ts"):
        if out.get(key) is not None:
            out[key] = out[key].strftime("%H:%M:%S")
    return out


def calc_recent_price_trend(history, lookback=5):
    """% change of underlying LTP over the last `lookback` stored cycles.
    Cheap momentum proxy for gating PCR-based bias -- NOT a substitute for real
    candle/ATR confirmation (that's a bigger future upgrade), but enough to stop
    PCR alone from claiming a directional bias while price is actually flat/against it."""
    pts = list(history)
    if len(pts) < 2:
        return None
    window = pts[-lookback:] if len(pts) >= lookback else pts
    first_ltp, last_ltp = window[0].get("ltp"), window[-1].get("ltp")
    if not first_ltp or not last_ltp:
        return None
    return round((last_ltp - first_ltp) / first_ltp * 100, 3)


def get_symbol_bucket(symbol, key, factory):
    d = state[key]
    if symbol not in d:
        d[symbol] = factory()
    return d[symbol]


def apply_fake_signal_filter(symbol, signal, bias):
    """
    Smart fake-signal filter -- reduces whipsaw trades before they reach paper
    trading. Two checks, both must pass for a signal to stay 'tradeable':
      1. Persistence: this bias must have held for BIAS_PERSISTENCE_SECONDS of
         real time (not cycle-count -- a cycle-count filter meant the actively
         VIEWED symbol at 7s refresh only needed 14s to "confirm" while a
         background symbol at 45s refresh needed 90s for the identical setting.
         Time-based persistence behaves the same regardless of refresh rate).

         NEUTRAL GRACE PERIOD: real production data (2026-07-14, NIFTY, 1541
         cycles) showed the underlying OI-wall bias genuinely flickering back to
         NEUTRAL/RANGE for a single 7-50s cycle before immediately resuming the
         SAME direction -- e.g. BULLISH BIAS reappeared 5+ times across the
         morning, and a 95%-confidence BEARISH BREAKDOWN (a real OI-wall signal,
         not the flimsy PCR-only branch) got wiped out by one NEUTRAL blip at
         45s, just under the 60s bar -- NOT ONCE all day did any bias survive
         60 continuous seconds without a reset. A strict "must be byte-identical
         every single cycle" persistence check is too fragile against this kind
         of natural 1-cycle noise. Fix: a brief NEUTRAL reading no longer resets
         the streak immediately -- it's tolerated for NEUTRAL_GRACE_SECONDS, and
         only a genuinely OPPOSITE directional bias (or NEUTRAL outlasting the
         grace window) triggers a real reset.
      2. Cooldown: no new entry within COOLDOWN_MINUTES_AFTER_SL of the last
         stop-loss on this symbol (kills revenge-trading into a choppy market).
    """
    now = dt.datetime.now()
    streak = state["bias_streak_by_symbol"].setdefault(
        symbol, {"bias": None, "since": None, "alerted": False, "neutral_since": None}
    )
    is_neutral = ("NEUTRAL" in bias) or ("PINNING" in bias)

    if is_neutral:
        if streak["bias"] is not None:
            if streak["neutral_since"] is None:
                streak["neutral_since"] = now   # start the grace clock, don't reset yet
            elif (now - streak["neutral_since"]).total_seconds() > NEUTRAL_GRACE_SECONDS:
                # been neutral too long -- genuinely reset, this wasn't a blip
                streak.update({"bias": None, "since": None, "alerted": False, "neutral_since": None})
    elif streak["bias"] == bias:
        streak["neutral_since"] = None   # same direction resumed -- clear the grace marker, keep accumulating
    else:
        # genuinely different directional bias (or first-ever reading) -- real reset
        streak.update({"bias": bias, "since": now, "alerted": False, "neutral_since": None})

    held_seconds = (now - streak["since"]).total_seconds() if streak["since"] else 0

    if signal.get("tradeable"):
        if held_seconds < BIAS_PERSISTENCE_SECONDS:
            signal["tradeable"] = False
            signal["filter_reason"] = f"Bias held only {held_seconds:.0f}s (need {BIAS_PERSISTENCE_SECONDS}s) -- waiting for persistence."
            return signal

        cooldown_until = state["cooldown_until_by_symbol"].get(symbol)
        if cooldown_until and now < cooldown_until:
            signal["tradeable"] = False
            remaining = round((cooldown_until - now).total_seconds() / 60, 1)
            signal["filter_reason"] = f"Cooldown after recent stop-loss -- {remaining} min remaining."
    return signal


def _ollama_background_task(symbol, payload):
    """Runs in its OWN background thread (socketio.start_background_task), never
    inline in run_symbol_loop -- a slow Ollama call (up to ~50s cold) must never
    delay that symbol's next live-data cycle. Fire-and-forget: writes result to
    state when done, next dashboard payload picks it up."""
    try:
        insight = get_ai_insight(payload)
        if insight:
            state["ollama_insight_by_symbol"][symbol] = {"insight": insight, "last_refresh": dt.datetime.now()}
            log.info(f"Ollama insight ready for {symbol}: {insight.get('confidence_label')} confidence, bias={insight.get('market_bias')}")
    except Exception as e:
        log.warning(f"Ollama background task failed for {symbol}: {e}")
    finally:
        state["ollama_request_in_flight"].discard(symbol)


def run_symbol_loop(symbol, angel, nse, bse):
    """One independent loop per symbol -- all run concurrently as separate
    background tasks so backtest data accumulates for every symbol regardless
    of what's shown on screen. Any symbol with at least one active viewer
    (state["symbol_viewers"][symbol] non-empty) refreshes at full
    REFRESH_INTERVAL speed for live scalping; every other symbol refreshes
    at the slower BACKGROUND_REFRESH_SECONDS to stay well within Angel
    One's rate limits while still building backtest history."""
    cfg = SYMBOLS[symbol]

    while True:
        cycle_start = time.time()
        is_active_view = bool(state["symbol_viewers"].get(symbol))

        open_now, closed_reason = is_market_open(cfg)
        dev_mode_now = state["dev_settings"]["DEV_MODE_WHEN_CLOSED"]
        if not open_now and not dev_mode_now:
            log.info(f"Market closed ({closed_reason}) for {symbol} -- skipping API calls, sleeping {CLOSED_SLEEP_SECONDS}s to save credits.")
            status_payload = {"symbol": symbol, "open": False, "reason": closed_reason, "checked_at": now_ist().strftime("%H:%M:%S IST")}
            state["market_status_by_symbol"][symbol] = status_payload
            if is_active_view:
                state["market_status"] = status_payload
                socketio.emit("market_status", status_payload)
            socketio.sleep(CLOSED_SLEEP_SECONDS)
            continue
        elif not open_now and dev_mode_now:
            log.info(f"Market closed ({closed_reason}) for {symbol} -- DEV_MODE_WHEN_CLOSED is on, fetching anyway at throttled rate ({DEV_MODE_REFRESH_SECONDS}s) for testing. Data will be Friday's last-known values, not live.")
            status_payload = {"symbol": symbol, "open": False, "reason": f"{closed_reason} (dev mode -- showing last-known data)", "checked_at": now_ist().strftime("%H:%M:%S IST"), "dev_mode": True}
            state["market_status_by_symbol"][symbol] = status_payload
            if is_active_view:
                state["market_status"] = status_payload
                socketio.emit("market_status", status_payload)
        else:
            state["market_status_by_symbol"][symbol] = {"symbol": symbol, "open": True}
            if is_active_view:
                state["market_status"] = {"symbol": symbol, "open": True}

        try:
            if cfg["type"] in COMMODITY_TYPES:
                underlying, _ = angel.get_commodity_underlying(symbol)
            else:
                underlying = angel.get_index_spot_ltp(symbol)

            if not underlying:
                log.warning(f"No Angel One LTP this cycle for {symbol}.")
                socketio.sleep(REFRESH_INTERVAL if is_active_view else BACKGROUND_REFRESH_SECONDS)
                continue

            # Milestone 20, Phase 6: feed this cycle's already-fetched LTP
            # into the in-process 1m/3m/5m candle recorder -- zero extra
            # broker calls (reuses the `underlying` value just fetched
            # above via the shared `angel` session), best-effort so a bug
            # here can never break the real cycle (see candle_recorder.py's
            # own docstring for why this exists: data_access.load_candles()'s
            # archive only updates once a day, staling structure_alerts.py's
            # reversal detection for hours).
            try:
                candle_recorder.append_tick(symbol, dt.datetime.now(), underlying)
            except Exception as e:
                log.warning(f"candle_recorder.append_tick failed for {symbol}: {e}")

            now_str = dt.datetime.now().strftime("%H:%M:%S")
            history = get_symbol_bucket(symbol, "history_by_symbol", lambda: deque(maxlen=MAX_HISTORY_POINTS))

            cached_structure = state["market_structure_by_symbol"].get(symbol)
            last_attempt = state.setdefault("market_structure_last_attempt", {}).get(symbol)
            attempt_due = not last_attempt or (dt.datetime.now() - last_attempt).total_seconds() >= 120
            if attempt_due and (not cached_structure or cached_structure.get("computed_date") != now_ist().date().isoformat()):
                state["market_structure_last_attempt"][symbol] = dt.datetime.now()
                try:
                    cand_token, cand_exch = angel.get_underlying_token_for_candles(symbol, cfg)
                    if cand_token:
                        candles = angel.get_historical_candles(cand_token, cand_exch, interval="THREE_MINUTE", days=5)
                        # Production hardening: heals candle_recorder.py's
                        # own 3m history from whatever downtime happened
                        # (VPS reboot, crash-restart) using this SAME
                        # already-fetched broker data -- zero new API
                        # calls. This is the one caller of
                        # reconcile_from_broker_candles(); best-effort,
                        # a failure here must never break the real
                        # market-structure computation below.
                        try:
                            candle_recorder.reconcile_from_broker_candles(symbol, "3m", candles)
                        except Exception as e:
                            log.warning(f"candle_recorder.reconcile_from_broker_candles failed for {symbol}: {e}")
                        structure = build_market_structure(candles)
                        if structure["candle_count"] > 0:
                            # Only mark today as "done" on a genuine success -- a
                            # rate-limit/empty response must retry (after the 2min
                            # cooldown) not silently go without structure all day.
                            structure["computed_date"] = now_ist().date().isoformat()
                            state["recent_candles_by_symbol"][symbol] = structure.pop("recent_candles", [])
                            state["market_structure_by_symbol"][symbol] = structure
                            # FULL (non-truncated) candle list for the Ichimoku engine --
                            # needs >=120 (300+ recommended) candles for a real cloud,
                            # far more than recent_candles_by_symbol's 30-candle slice
                            # above; same already-fetched `candles`, zero extra API calls.
                            state["ichimoku_candles_by_symbol"][symbol] = candles
                            log.info(f"Market structure refreshed for {symbol}: {structure['candle_count']} candles, ATR={structure['atr_14']}")
                            save_market_structure_snapshot(symbol, structure)
                        else:
                            log.warning(f"Market structure fetch for {symbol} returned 0 candles -- will retry in ~2min (not marking as done).")
                except Exception as e:
                    log.warning(f"Market structure refresh failed for {symbol}: {e}")

            # Dynamic S/R Engine (V1, PDH/PDL-based) -- every cycle (cheap:
            # reuses candles/prev_day already fetched above for market
            # structure, no extra API calls), so its ladder/signal track the
            # fresh LTP even between the 2-min market-structure refreshes.
            try:
                ms_for_dsr = state["market_structure_by_symbol"].get(symbol)
                candles_for_dsr = state["recent_candles_by_symbol"].get(symbol)
                prev_day = ms_for_dsr.get("prev_day") if ms_for_dsr else None
                if prev_day and candles_for_dsr:
                    state["dynamic_sr_by_symbol"][symbol] = evaluate_dynamic_sr(
                        candles_for_dsr, prev_day["pdh"], prev_day["pdl"], current_price=underlying)
            except Exception as e:
                log.warning(f"Dynamic S/R Engine computation failed for {symbol}: {e}")

            if cfg["type"] == "index_spot":
                # No option chain (e.g. INDIA VIX) -- just push spot price.
                history.append({"time": now_str, "ltp": underlying, "pcr": None})
                spot_payload = {
                    "symbol": symbol, "chain_available": False, "updated": now_str,
                    "ltp": underlying, "history": list(history),
                }
                state["last_payload_by_symbol"][symbol] = spot_payload
                if is_active_view:
                    socketio.emit("update", spot_payload, room=symbol)
                log.info(f"Updated (spot-only) | {symbol} LTP={underlying}")
                elapsed = time.time() - cycle_start
                effective_interval = (REFRESH_INTERVAL if is_active_view else BACKGROUND_REFRESH_SECONDS) if open_now else DEV_MODE_REFRESH_SECONDS
                socketio.sleep(max(1, effective_interval - elapsed))
                continue

            prev_token_state = get_symbol_bucket(symbol, "prev_token_state_by_symbol", dict)
            rows, atm = build_strike_rows(angel, symbol, underlying, cfg["step"], cfg, prev_token_state)

            nse_status = "N/A for this instrument"
            nse_cross_check = None
            nse_data = None
            secondary_source = None
            if cfg["type"] == "index_option" and cfg["exch"] == "NSE":
                secondary_source = "NSE"
            elif cfg["type"] == "index_option" and cfg["exch"] == "BSE":
                secondary_source = "BSE"

            # PERFORMANCE: cross-check is "best-effort secondary" (website-
            # scraping, not the authoritative source for any trading
            # decision) -- throttle to once every 5 cycles instead of every
            # cycle, reusing the previous result in between. Angel One
            # (primary) LTP/option-chain fetching above is UNCHANGED and
            # still runs every cycle at full speed.
            if secondary_source:
                cross_check_bucket = state.setdefault("cross_check_cycle_count", {})
                cycle_count = cross_check_bucket.get(symbol, 0)
                should_fetch_cross_check = (cycle_count % 5 == 0)
                cross_check_bucket[symbol] = cycle_count + 1

                if should_fetch_cross_check:
                    wanted, _ = wanted_strikes(underlying, cfg["step"], STRIKES_EACH_SIDE)
                    if secondary_source == "NSE":
                        nse_data = nse.get_cross_check(symbol, wanted)
                    else:
                        nse_data = bse.get_cross_check(symbol, STRIKES_EACH_SIDE)
                    state.setdefault("last_cross_check_by_symbol", {})[symbol] = nse_data
                else:
                    nse_data = state.get("last_cross_check_by_symbol", {}).get(symbol)

            if secondary_source:
                if nse_data:
                    nse_status = "OK"
                    ltp_diff_pct = round(abs(nse_data["underlying"] - underlying) / underlying * 100, 3) if underlying else 0
                    pcr_here = calc_pcr(rows)
                    pcr_diff = round(abs(nse_data["pcr"] - pcr_here), 3)
                    nse_cross_check = {
                        "source": secondary_source,
                        "nse_underlying": nse_data["underlying"], "angel_underlying": underlying,
                        "ltp_diff_pct": ltp_diff_pct,
                        "nse_pcr": nse_data["pcr"], "angel_pcr": pcr_here, "pcr_diff": pcr_diff,
                        "nse_total_ce_oi": nse_data["total_ce_oi"], "nse_total_pe_oi": nse_data["total_pe_oi"],
                        "expiry": nse_data["expiry"],
                        "mismatch_warning": ltp_diff_pct > 0.5 or pcr_diff > 0.3,
                    }
                    if nse_cross_check["mismatch_warning"]:
                        log.warning(f"{secondary_source}/Angel mismatch for {symbol}: LTP diff {ltp_diff_pct}%, PCR diff {pcr_diff}")
                else:
                    nse_status = "Blocked/unavailable"
            if is_active_view:
                state["nse_status"] = f"{secondary_source}: {nse_status}" if secondary_source else nse_status

            pcr = calc_pcr(rows)
            max_pain = calc_max_pain(rows)
            support, resistance = oi_walls(rows)
            price_trend_pct = calc_recent_price_trend(history)
            bias, note = detect_bias(rows, atm, pcr, price_trend_pct=price_trend_pct,
                                      underlying=underlying, market_structure=state["market_structure_by_symbol"].get(symbol))
            note = f"{note}"

            # OI-Buildup Conviction-Strength: honest count of how many independent
            # factors agree on direction (see oi_engine.compute_conviction_strength) --
            # not a magnitude/price-target prediction.
            conviction = None
            atm_row_for_conviction = next((r for r in rows if r.strike == atm), None)
            if atm_row_for_conviction is not None:
                atm_ce_vol_bucket = state.setdefault("atm_ce_vol_history", {}).setdefault(symbol, deque(maxlen=20))
                atm_pe_vol_bucket = state.setdefault("atm_pe_vol_history", {}).setdefault(symbol, deque(maxlen=20))
                pcr_history_values = [h.get("pcr") for h in list(history)[-10:]]
                conviction = compute_conviction_strength(
                    ce_signal=atm_row_for_conviction.ce_signal, pe_signal=atm_row_for_conviction.pe_signal,
                    pcr_history=pcr_history_values, current_pcr=pcr,
                    ce_vol_history=list(atm_ce_vol_bucket), pe_vol_history=list(atm_pe_vol_bucket),
                    current_ce_vol=atm_row_for_conviction.ce_vol, current_pe_vol=atm_row_for_conviction.pe_vol,
                )
                atm_ce_vol_bucket.append(atm_row_for_conviction.ce_vol)
                atm_pe_vol_bucket.append(atm_row_for_conviction.pe_vol)

            nse_atm_row = None
            expiry_date_obj = None
            # Milestone 17+ audit fix: Angel One's own instrument master is
            # now the PRIMARY expiry source here (same one find_nearest_expiry()
            # already uses for Greeks-fetching below, and is_expiry_today()
            # uses for OI-noise-filtering in build_strike_rows) -- NSE's
            # expiry string is only a fallback when Angel One's lookup comes
            # back empty (e.g. instrument master mid-refresh). Previously
            # this block ONLY parsed NSE's expiry string, which meant every
            # MCX commodity symbol (never covered by NSE option-chain data)
            # got expiry_date_obj=None here always -- no expiry-day signal
            # weighting, no is_expiry_today flag -- even though a perfectly
            # good broker-sourced expiry was available the whole time.
            angel_expiry_str = angel.find_nearest_expiry(symbol)
            if angel_expiry_str:
                expiry_date_obj = parse_expiry(angel_expiry_str)
            if nse_data:
                nse_atm_row = nse_data["rows_by_strike"].get(atm)
                if expiry_date_obj is None:
                    for fmt in ("%d-%b-%Y", "%d %b %Y"):   # NSE: "14-Jul-2026", BSE: "16 Jul 2026"
                        try:
                            expiry_date_obj = dt.datetime.strptime(nse_data["expiry"], fmt).date()
                            break
                        except Exception:
                            continue

            # Live Greeks (Delta/Gamma/Theta/Vega/IV) -- confirmed-working via
            # Angel One's optionGreek endpoint (see explore_angelone_data.py).
            # Purely informational/supplementary -- never blocks or alters
            # any existing signal-logic if this fails or returns nothing.
            # THROTTLED to once per 60s per-symbol -- this endpoint returns
            # ALL strikes at once (a large response) and calling it every
            # single cycle (every few seconds) would risk Angel One's rate
            # limits, which we've already hit elsewhere this session.
            # Uses Angel One's OWN instrument-master for expiry (NOT
            # nse_data["expiry"]) -- MCX commodities never have NSE data
            # ("NSE=N/A for this instrument"), so relying on NSE's expiry
            # was silently skipping Greeks for every commodity symbol.
            # (Same value already resolved above as angel_expiry_str -- reused
            # here rather than re-scanning the instrument master a second time.)
            greeks_expiry = angel_expiry_str
            if greeks_expiry:
                greeks_bucket = state.setdefault("greeks_cache_by_symbol", {})
                last_fetch = greeks_bucket.get(symbol, {}).get("ts", 0)
                if time.time() - last_fetch >= 60:
                    greeks_by_strike = angel.get_option_greeks(symbol, greeks_expiry)
                    greeks_bucket[symbol] = {"ts": time.time(), "data": greeks_by_strike}
                else:
                    greeks_by_strike = greeks_bucket.get(symbol, {}).get("data", {})
                if greeks_by_strike:
                    for r in rows:
                        ce_g = greeks_by_strike.get((float(r.strike), "CE"))
                        if ce_g:
                            r.ce_delta, r.ce_gamma, r.ce_theta, r.ce_vega = ce_g["delta"], ce_g["gamma"], ce_g["theta"], ce_g["vega"]
                            if not r.ce_iv:   # prefer Angel's own IV if we didn't already have one from NSE/BSE
                                r.ce_iv = ce_g["iv"]
                        pe_g = greeks_by_strike.get((float(r.strike), "PE"))
                        if pe_g:
                            r.pe_delta, r.pe_gamma, r.pe_theta, r.pe_vega = pe_g["delta"], pe_g["gamma"], pe_g["theta"], pe_g["vega"]
                            if not r.pe_iv:
                                r.pe_iv = pe_g["iv"]

            signal = generate_signal(rows, atm, bias, note, pcr, support, resistance,
                                      nse_atm_row=nse_atm_row, underlying=underlying,
                                      expiry_date=expiry_date_obj, strike_step=cfg["step"],
                                      source_label=secondary_source or "NSE",
                                      market_structure=state["market_structure_by_symbol"].get(symbol))
            signal = apply_fake_signal_filter(symbol, signal, bias)

            # Expiry-day detection: purely mechanical date-comparison against
            # the already-fetched expiry_date_obj. Does NOT drive any
            # strategy/signal logic -- just surfaces the fact for the
            # trader's own awareness (existing tools like OI-Buildup
            # Conviction Strength remain the only signal sources).
            is_expiry_today = expiry_date_obj is not None and expiry_date_obj == now_ist().date()

            # Milestone 17+: attach this cycle's expiry-day OI/scalping
            # reading to the signal dict every symbol's signal carries --
            # pure/read-only (see expiry_intelligence.py's own module
            # docstring), never alters `signal`'s own action/confidence/
            # entry/target/SL fields above.
            days_to_expiry = (expiry_date_obj - now_ist().date()).days if expiry_date_obj else None
            signal["expiry_context"] = expiry_intelligence.compute_scalping_metrics(
                rows, underlying, days_to_expiry=days_to_expiry, atm=atm, step=cfg["step"],
            )

            # S/R probability + state machine computed BEFORE paper trading so the
            # trade-opening trigger (get_sr_trade_trigger) uses THIS cycle's fresh
            # state, not last cycle's -- this is now the SOLE trade-opening pathway,
            # the old oi_engine signal above is display/reference only.
            #
            # CRITICAL: only advance this when market is genuinely open. If we let
            # the state machine's persistence timer run on stale (market-closed)
            # data, the SAME last-known reading repeating every cycle could
            # satisfy the 45s persistence threshold purely by elapsed wall-clock
            # time -- not genuine sustained market behavior -- and could even
            # silently reach CONFIRMED/ACTIVE overnight. Freeze the state machine
            # entirely while closed; still show the raw probability numbers (not
            # state-gated) for reference, clearly marked as stale.
            if open_now:
                sr_table = build_sr_probability_table(
                    rows, atm, pcr, state["market_structure_by_symbol"].get(symbol), list(history), underlying,
                    (state["market_structure_by_symbol"].get(symbol) or {}).get("custom_levels"),
                    resistance_wall_confirmed=signal.get("wall_cross_verified") if signal.get("direction") == "CE" else None,
                    support_wall_confirmed=signal.get("wall_cross_verified") if signal.get("direction") == "PE" else None,
                )
                sr_state = advance_sr_state_machine(
                    symbol, sr_table, underlying, state["market_structure_by_symbol"].get(symbol), rows, atm, cfg["step"],
                    history=history,
                    resistance_wall_confirmed=signal.get("wall_cross_verified") if signal.get("direction") == "CE" else None,
                    support_wall_confirmed=signal.get("wall_cross_verified") if signal.get("direction") == "PE" else None,
                )
            else:
                sr_table = build_sr_probability_table(
                    rows, atm, pcr, state["market_structure_by_symbol"].get(symbol), list(history), underlying,
                    (state["market_structure_by_symbol"].get(symbol) or {}).get("custom_levels"),
                )
                if sr_table:
                    sr_table["data_stale"] = True
                sr_state = None   # frozen -- don't touch state["sr_state_by_symbol"] while closed

            if state["engine_v2_enabled"].get(symbol):
                try:
                    v2_result = build_v2_probability_table(
                        rows, atm, pcr, state["market_structure_by_symbol"].get(symbol), list(history), underlying, symbol=symbol
                    )
                    state["v2_by_symbol"][symbol] = v2_result
                except Exception as e:
                    log.warning(f"Engine V2 computation failed for {symbol} (V1 unaffected): {e}")

            if open_now:
                sr_trigger = get_sr_trade_trigger(symbol, state["market_structure_by_symbol"].get(symbol))
                paper = update_paper_trading(symbol, signal, rows, now_str, sr_trigger=sr_trigger,
                                              candles=state["recent_candles_by_symbol"].get(symbol))
                update_paper_orders(symbol, rows, now_str, cfg,
                                     candles=state["recent_candles_by_symbol"].get(symbol))
                if sr_trigger:
                    fanout_auto_trade_entry("SWING", symbol, cfg, sr_trigger, now_ist())
                log_cycle_to_db(symbol, now_ist(), underlying, atm, pcr, max_pain, bias, note, signal, rows)
            else:
                bucket = paper_trade_bucket(symbol)   # dev mode: show existing state, don't open/close trades on stale data
                total_trades = bucket["wins"] + bucket["losses"] + bucket["time_exits"]
                paper = {
                    "open_trade": None, "history": [{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]],
                    "wins": bucket["wins"], "losses": bucket["losses"], "time_exits": bucket["time_exits"],
                    "win_rate": round(bucket["wins"] / total_trades * 100, 1) if total_trades else 0.0,
                    "total_points": round(bucket["total_points"], 2),
                }
                signal["reason"] = f"[DEV MODE -- stale data, no trades taken] {signal.get('reason', '')}"
                signal["tradeable"] = False

            total_ce_oi = sum(r.ce_oi for r in rows)
            total_pe_oi = sum(r.pe_oi for r in rows)

            history.append({"time": now_str, "ltp": underlying, "pcr": pcr})

            alerts = get_symbol_bucket(symbol, "alerts_by_symbol", lambda: deque(maxlen=MAX_ALERTS))
            last_bias = state["last_bias_by_symbol"].get(symbol)
            streak = state["bias_streak_by_symbol"].get(symbol, {})
            held_seconds = (dt.datetime.now() - streak["since"]).total_seconds() if streak.get("since") else 0
            bias_just_confirmed = (
                streak.get("bias") == bias
                and held_seconds >= BIAS_PERSISTENCE_SECONDS
                and not streak.get("alerted")
            )
            if bias != last_bias and last_bias is not None and bias_just_confirmed:
                alert_msg = f"[{now_str}] {symbol} bias changed: {last_bias} -> {bias}. {note}"
                alerts.appendleft(alert_msg)
                if is_active_view:
                    socketio.emit("alert", {"message": alert_msg})
                send_telegram(f"{symbol} ALERT: {alert_msg}")
                state["last_bias_by_symbol"][symbol] = bias
                streak["alerted"] = True
            elif last_bias is None:
                state["last_bias_by_symbol"][symbol] = bias   # first cycle, just set baseline, no alert

            # Milestone 14, Phase 2: automatic Intelligence History
            # logging + alert evaluation -- off by default
            # (INTELLIGENCE_ALERTS_AUTO_ENABLED), only on genuinely live
            # data (open_now, never the dev-mode/stale branch above), and
            # isolated in its own try/except so a failure here can never
            # affect paper trading, signal generation, or any other
            # engine in this loop. See _run_intelligence_alerts_auto_cycle's
            # own docstring for the full read-only/non-trading guarantee.
            if open_now and agents_config.INTELLIGENCE_ALERTS_AUTO_ENABLED:
                try:
                    _run_intelligence_alerts_auto_cycle(symbol)
                except Exception as e:
                    log.warning(f"Intelligence alerts auto-cycle failed for {symbol} (main loop unaffected): {e}")

            # Ichimoku Engine (ADVISORY/DISPLAY ONLY -- see ichimoku_engine.py's
            # module docstring; does NOT gate fanout_auto_trade_entry / real
            # orders). Toggle-controlled kill-switch, ON by default. Reuses the
            # FULL candle history cached above (zero extra API calls), and its
            # own SAME code path is what backtest.py's simulate_ichimoku_trades
            # replays, so live and backtest never diverge. try/except-isolated
            # so a failure here can never break any other engine (same posture
            # as S/R Engine V3 below).
            ichimoku_signal, ichimoku_paper = None, None
            if state.get("ichimoku_engine_enabled"):
                ichimoku_candles = state["ichimoku_candles_by_symbol"].get(symbol)
                if ichimoku_candles and len(ichimoku_candles) >= 120:
                    try:
                        ichimoku_signal = ichimoku_analyze(ichimoku_candles, today=now_ist().date())
                        state["ichimoku_signal_by_symbol"][symbol] = ichimoku_signal
                        if open_now:
                            ichimoku_paper = update_ichimoku_paper_trading(symbol, ichimoku_signal, underlying, now_str)
                    except Exception as e:
                        log.warning(f"Ichimoku Engine computation failed for {symbol} (other engines unaffected): {e}")

            # New Trend Meter (EXPERIMENTAL, toggle-controlled) -- computed
            # ALONGSIDE the existing trend_meter, never replacing it. See
            # oi_engine.compute_new_trend_meter for the honest limitations.
            # ON by default since 2026-08-04 -- now fuses `ichimoku_signal`
            # (trend-confirmation, regime-weighted) + market_structure regime +
            # is_expiry_today when available; falls back to the exact original
            # 5-factor formula on any cycle where ichimoku_signal is None.
            new_trend_meter = None
            if state.get("new_trend_meter_enabled"):
                ms_for_ntm = state["market_structure_by_symbol"].get(symbol) or {}
                pcr_history_for_ntm = [h.get("pcr") for h in list(history)[-10:]]
                new_trend_meter = compute_new_trend_meter(
                    rows, atm, pcr, pcr_history_for_ntm, underlying,
                    vwap=ms_for_ntm.get("vwap"), step=cfg.get("step", 50),
                    ichimoku=ichimoku_signal, market_structure=ms_for_ntm, is_expiry_today=is_expiry_today,
                )

            # Scalping Engine (EXPERIMENTAL, toggle-controlled, ADVISORY ONLY --
            # produces a signal dict only, never opens a paper/real trade on its
            # own; see scalping_engine.py's module docstring). Computed
            # alongside the existing signal engines, never replacing them. Free
            # to run every cycle: reuses this cycle's already-fetched
            # rows/market_structure, adds zero extra network/DB calls.
            scalp_signal = None
            if state.get("scalp_engine_enabled") and rows:
                premium_buckets = state["scalp_premium_history"].setdefault(symbol, {})
                volume_buckets = state["scalp_volume_history"].setdefault(symbol, {})
                scalp_signal = generate_scalp_signal(
                    rows, atm, cfg.get("step", 50), underlying,
                    state["market_structure_by_symbol"].get(symbol),
                    premium_buckets, volume_buckets, expiry_date_obj,
                )
                state["scalp_signal_by_symbol"][symbol] = scalp_signal
                # Append THIS cycle's own reading AFTER evaluation, never before
                # -- evaluate_scalp_candidate's contract requires prior-readings-
                # only history (otherwise "broke the recent high" would be
                # self-referentially satisfied by the reading being evaluated).
                row_by_strike = {r.strike: r for r in rows}
                for direction in ("CE", "PE"):
                    strike = (scalp_signal.get(direction) or {}).get("strike")
                    row = row_by_strike.get(strike) if strike else None
                    if not row:
                        continue
                    key = (strike, direction)
                    ltp = row.ce_ltp if direction == "CE" else row.pe_ltp
                    vol = row.ce_vol if direction == "CE" else row.pe_vol
                    p_bucket = premium_buckets.setdefault(key, [])
                    p_bucket.append({"ltp": ltp, "time": now_str})
                    del p_bucket[:-30]   # cap history length, oldest-first
                    v_bucket = volume_buckets.setdefault(key, [])
                    v_bucket.append(vol)
                    del v_bucket[:-30]

            # Scalp paper-trading -- own separate bucket/win-rate, gated the
            # same way as the S/R engine's (only advances on genuinely live,
            # market-open data; frozen while closed, same reasoning as the
            # S/R state machine above: letting it run on stale repeated data
            # would satisfy time-based exits/cooldowns from wall-clock time
            # alone, not genuine market behavior).
            scalp_paper = None
            if state.get("scalp_engine_enabled") and open_now:
                scalp_paper = update_scalp_paper_trading(symbol, scalp_signal, rows, now_str)
                scalp_candidate = select_best_scalp_candidate(scalp_signal)
                if scalp_candidate:
                    fanout_auto_trade_entry("SCALP", symbol, cfg, scalp_candidate, now_ist())

            # S/R Engine V3 (EXPERIMENTAL, per-symbol toggle-controlled -- see
            # sr_engine_v3.py's module docstring). Computed alongside the
            # existing engines, never replacing them; wrapped in its own
            # try/except so a V3 bug can never break V1/V2/Scalp/live trading.
            v3_signal, v3_paper = None, None
            if state["v3_engine_enabled"].get(symbol) and rows:
                try:
                    prev_day_validation = get_v3_previous_day_validation(symbol, cfg)
                    today_ltp_hist = get_v3_today_ltp_history(symbol, underlying)
                    v3_volume_history_by_key = {}
                    if prev_day_validation:
                        for side, direction in (("support", "PE"), ("resistance", "CE")):
                            cluster = (prev_day_validation.get(side) or {}).get("cluster")
                            strike = cluster.get("center_strike") if cluster else None
                            if strike is not None:
                                row = next((r for r in rows if r.strike == strike), None)
                                cur_vol = (row.ce_vol if direction == "CE" else row.pe_vol) if row else None
                                v3_volume_history_by_key[(strike, direction)] = get_v3_volume_history(
                                    symbol, strike, direction, cur_vol)

                    v3_weights = get_v3_adaptive_weights(symbol)
                    v3_wall_history = state["v3_wall_center_history_by_symbol"].setdefault(
                        symbol, {"support": deque(maxlen=30), "resistance": deque(maxlen=30)})

                    v3_signal = generate_v3_signal(
                        rows, underlying, state["market_structure_by_symbol"].get(symbol), cfg.get("step", 50),
                        prev_day_validation=prev_day_validation, today_ltp_history=today_ltp_hist,
                        volume_history_by_key=v3_volume_history_by_key, expiry_date=expiry_date_obj, now=now_ist(),
                        factor_weights=v3_weights,
                        wall_center_history={"support": list(v3_wall_history["support"]),
                                              "resistance": list(v3_wall_history["resistance"])},
                    )
                    # Append THIS cycle's cluster centers AFTER evaluation, never
                    # before (same prior-readings-only contract as
                    # compute_premium_entry_trigger elsewhere in this codebase).
                    if v3_signal.get("support_cluster_center") is not None:
                        v3_wall_history["support"].append(v3_signal["support_cluster_center"])
                    if v3_signal.get("resistance_cluster_center") is not None:
                        v3_wall_history["resistance"].append(v3_signal["resistance_cluster_center"])
                    state["v3_signal_by_symbol"][symbol] = v3_signal
                    if open_now:
                        # NOTE: market_structure_by_symbol[symbol] never actually has a
                        # "recent_candles" key -- it's `.pop()`'d out into
                        # recent_candles_by_symbol before market_structure_by_symbol is
                        # populated (see the market-structure refresh block above), so
                        # the old `.get("recent_candles")` here was always None and
                        # should_pause_time_exit's guard never actually fired. Pull from
                        # the dict that genuinely holds it instead.
                        v3_candles = state["recent_candles_by_symbol"].get(symbol)
                        v3_paper = update_v3_paper_trading(symbol, v3_signal, rows, now_str, candles=v3_candles)
                except Exception as e:
                    log.warning(f"S/R Engine V3 computation failed for {symbol} (V1/V2/Scalp unaffected): {e}")

            payload = {
                "symbol": symbol, "chain_available": True, "updated": now_str,
                "ltp": underlying, "atm": atm, "pcr": pcr, "max_pain": max_pain,
                "bias": bias, "note": note, "nse_status": nse_status, "secondary_source": secondary_source, "nse_cross_check": nse_cross_check,
                "market_structure": state["market_structure_by_symbol"].get(symbol),
                "trend_meter": compute_trend_meter(bias, pcr, signal, state["market_structure_by_symbol"].get(symbol)),
                "new_trend_meter": new_trend_meter,
                "scalp_signal": scalp_signal,
                "scalp_paper": scalp_paper,
                "v3_signal": v3_signal,
                "v3_paper": v3_paper,
                "ichimoku_signal": ichimoku_signal,
                "ichimoku_paper": ichimoku_paper,
                "conviction_strength": conviction,
                "is_expiry_today": is_expiry_today,
                "expiry_date": expiry_date_obj.isoformat() if expiry_date_obj else None,
                "expiry_context": signal.get("expiry_context"),
                "sr_probability": sr_table,
                "sr_state_machine": sr_state,
                "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
                "support": [{"strike": s.strike, "oi": s.pe_oi} for s in support],
                "resistance": [{"strike": r.strike, "oi": r.ce_oi} for r in resistance],
                "rows": [
                    {
                        "strike": r.strike, "atm": r.strike == atm,
                        "ce_oi": r.ce_oi, "ce_oi_chg": r.ce_oi_chg, "ce_vol": r.ce_vol,
                        "ce_ltp": r.ce_ltp, "ce_chg_pct": r.ce_chg_pct, "ce_signal": r.ce_signal,
                        "pe_oi": r.pe_oi, "pe_oi_chg": r.pe_oi_chg, "pe_vol": r.pe_vol,
                        "pe_ltp": r.pe_ltp, "pe_chg_pct": r.pe_chg_pct, "pe_signal": r.pe_signal,
                    }
                    for r in rows
                ],
                "history": list(history),
                "alerts": list(alerts),
                "signal": signal,
                "paper": paper,
            }

            if COMMENTARY_ENABLED and is_active_view and open_now:
                cached_commentary = state["commentary_by_symbol"].get(symbol)
                needs_refresh = (
                    not cached_commentary
                    or (dt.datetime.now() - cached_commentary["last_refresh"]).total_seconds() >= COMMENTARY_REFRESH_SECONDS
                )
                if needs_refresh:
                    text = get_commentary(payload, state["commentary_seen_symbols"])
                    if text:
                        state["commentary_by_symbol"][symbol] = {"text": text, "last_refresh": dt.datetime.now()}
            commentary_entry = state["commentary_by_symbol"].get(symbol)
            payload["chatgpt_commentary"] = commentary_entry["text"] if commentary_entry else None

            if OLLAMA_ENABLED and is_active_view and open_now and symbol not in state["ollama_request_in_flight"]:
                cached_insight = state["ollama_insight_by_symbol"].get(symbol)
                needs_refresh = (
                    not cached_insight
                    or (dt.datetime.now() - cached_insight["last_refresh"]).total_seconds() >= OLLAMA_REFRESH_SECONDS
                )
                if needs_refresh:
                    state["ollama_request_in_flight"].add(symbol)
                    socketio.start_background_task(_ollama_background_task, symbol, dict(payload))
            ollama_entry = state["ollama_insight_by_symbol"].get(symbol)
            payload["ollama_insight"] = ollama_entry["insight"] if ollama_entry else None

            state["last_payload_by_symbol"][symbol] = payload
            if is_active_view:
                socketio.emit("update", payload, room=symbol)
            log.info(f"Updated | {symbol} LTP={underlying} ATM={atm} PCR={pcr} "
                     f"[Reference-Only] Bias={bias} Signal={signal.get('action')} Conf={signal.get('confidence','-')} "
                     f"NSE={nse_status}{'' if is_active_view else ' [background]'}")

        except Exception as e:
            log.exception(f"Cycle error for {symbol}: {e}")

        elapsed = time.time() - cycle_start
        effective_interval = (REFRESH_INTERVAL if is_active_view else BACKGROUND_REFRESH_SECONDS) if open_now else DEV_MODE_REFRESH_SECONDS
        socketio.sleep(max(1, effective_interval - elapsed))


_shared_angel_fetcher = None   # set once start_all_symbol_loops() runs; reused by
                                # _get_position_monitor_angel() below so routes don't
                                # open a second, independent broker login/session.


def start_all_symbol_loops():
    """Spawns one independent background task per symbol, sharing single Angel
    One / NSE / BSE fetcher instances (one login session, one set of caches --
    NOT one per symbol, which would blow past session/rate limits). Loops are
    staggered on startup so ~14 symbols don't all hit the APIs in the same instant."""
    global _shared_angel_fetcher
    angel = AngelOneFetcher()
    _shared_angel_fetcher = angel
    nse = NSEFetcher()
    bse = BSEFetcher()

    for i, symbol in enumerate(SYMBOLS.keys()):
        stagger_delay = i * SYMBOL_STARTUP_STAGGER_SECONDS
        socketio.start_background_task(_delayed_symbol_loop, symbol, angel, nse, bse, stagger_delay)
        log.info(f"Scheduled background tracking for {symbol} (stagger +{stagger_delay:.1f}s)")


def _delayed_symbol_loop(symbol, angel, nse, bse, delay):
    if delay:
        socketio.sleep(delay)
    run_symbol_loop(symbol, angel, nse, bse)


# ----------------------------------------------------------------------------
# ACCOUNTS / AUTH / ADMIN ROUTES
# ----------------------------------------------------------------------------

def _safe_next_path(raw):
    """Only allow redirecting to a same-site relative path after login --
    prevents an open-redirect via a crafted ?next= value."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if g.user is not None:
        return redirect("/")
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not email or "@" not in email:
            error = "Enter a valid email address."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            error = auth.validate_password_strength(password)

        if not error:
            conn = sqlite3.connect(DB_PATH)
            try:
                existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                if existing:
                    error = "An account with this email already exists. Try logging in instead."
                else:
                    now_str = now_ist().isoformat()
                    cur = conn.execute(
                        """INSERT INTO users (email, password_hash, role, is_verified, is_suspended,
                                               created_at, updated_at)
                           VALUES (?,?, 'subscriber', 0, 0, ?, ?)""",
                        (email, auth.hash_password(password), now_str, now_str),
                    )
                    user_id = cur.lastrowid
                    raw_token, token_hash = auth.generate_verification_token()
                    conn.execute(
                        "UPDATE users SET verification_token_hash=?, verification_token_expires_at=? WHERE id=?",
                        (token_hash, auth.token_expiry().isoformat(), user_id),
                    )
                    conn.commit()
                    auth.send_email(
                        email, "Verify your account",
                        auth.verification_email_body(raw_token, request.host_url),
                    )
                    log.info(f"New registration: {email} (user_id={user_id}, pending verification)")
                    return render_template("register.html", success=True)
            finally:
                conn.close()
    return render_template("register.html", error=error, success=False)


@app.route("/verify-email/<token>")
def verify_email_page(token):
    token_hash = auth.hash_token(token)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM users WHERE verification_token_hash=?", (token_hash,)).fetchone()
        if row is None:
            return render_template("verify_result.html", ok=False,
                                    message="This verification link is invalid. It may have already been used.")
        expires_at = row["verification_token_expires_at"]
        if not expires_at or now_ist() > dt.datetime.fromisoformat(expires_at):
            return render_template("verify_result.html", ok=False,
                                    message="This verification link has expired. Ask an administrator to resend it.")
        now_str = now_ist().isoformat()
        trial_ends = now_ist() + dt.timedelta(days=auth.TRIAL_DAYS)
        conn.execute(
            """UPDATE users SET is_verified=1, verification_token_hash=NULL,
                                 verification_token_expires_at=NULL,
                                 trial_started_at=?, trial_ends_at=?, updated_at=?
               WHERE id=?""",
            (now_str, trial_ends.isoformat(), now_str, row["id"]),
        )
        conn.commit()
        log.info(f"Email verified for user_id={row['id']} ({row['email']}) -- {auth.TRIAL_DAYS}-day trial started.")
        return render_template("verify_result.html", ok=True,
                                message=f"Your email is verified! Your {auth.TRIAL_DAYS}-day free trial has started.")
    finally:
        conn.close()


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if g.user is not None:
        return redirect("/")
    error = None
    next_path = _safe_next_path(request.values.get("next"))
    if request.method == "POST":
        login_id = (request.form.get("login_id") or "").strip()
        password = request.form.get("password") or ""
        next_path = _safe_next_path(request.form.get("next"))
        ip = request.remote_addr
        locked, remaining = auth.is_login_locked(ip, login_id)
        if locked:
            error = f"Too many failed attempts. Try again in {remaining}s."
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE email=? OR username=?", (login_id.lower(), login_id)
                ).fetchone()
                if row is None or not auth.verify_password(password, row["password_hash"]):
                    auth.record_login_failure(ip, login_id)
                    error = "Invalid email/username or password."
                elif row["is_suspended"]:
                    error = f"This account is suspended. Call {SUPPORT_PHONE_NUMBER} for help."
                elif not row["is_verified"]:
                    error = "Please verify your email before logging in (check your inbox, or ask an administrator to resend the link)."
                else:
                    auth.reset_login_failures(ip, login_id)
                    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_ist().isoformat(), row["id"]))
                    conn.commit()
                    session.clear()
                    session["user_id"] = row["id"]
                    session.permanent = True
                    return redirect(next_path)
            finally:
                conn.close()
    return render_template("login.html", error=error, next_path=next_path)


@app.route("/api/auth/google", methods=["POST"])
def api_auth_google():
    """Verifies a Firebase ID token (minted client-side by the "Sign in with
    Google" button, via the Firebase JS SDK's signInWithPopup) and logs the
    user in -- creating an account on first sign-in. Google has already
    verified the user's email ownership at this point, so accounts created
    this way are auto-verified (is_verified=1, trial starts immediately) --
    this is what fixes "verification email not received" for anyone who
    uses this path instead of the password + emailed-link flow. Public by
    design (this IS a login mechanism, same as /login) -- the trust boundary
    is the server-side signature verification below, not session auth."""
    if not GOOGLE_SIGNIN_ENABLED:
        return jsonify({"error": "Google Sign-In is not configured on this server."}), 503
    data = request.get_json(force=True) or {}
    raw_id_token = data.get("id_token")
    if not raw_id_token:
        return jsonify({"error": "id_token is required."}), 400

    try:
        claims = google_id_token.verify_firebase_token(
            raw_id_token, google_auth_requests.Request(), audience=FIREBASE_PROJECT_ID,
        )
    except Exception as e:
        log.warning(f"Google Sign-In: ID token verification failed: {e}")
        return jsonify({"error": "Could not verify Google sign-in. Please try again."}), 401
    if claims is None:
        return jsonify({"error": "Could not verify Google sign-in. Please try again."}), 401

    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified"):
        return jsonify({"error": "Your Google account's email is not verified. Please use a verified Google account."}), 400

    now = now_ist()
    now_str = now.isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            trial_ends = now + dt.timedelta(days=auth.TRIAL_DAYS)
            # Random, never-shared password hash -- this account can only ever
            # log in via Google Sign-In (password-flow login still verifies
            # against password_hash, but nobody knows this random value).
            cur = conn.execute(
                """INSERT INTO users (email, password_hash, role, is_verified, is_suspended,
                                       trial_started_at, trial_ends_at, created_at, updated_at, last_login_at)
                   VALUES (?,?, 'subscriber', 1, 0, ?, ?, ?, ?, ?)""",
                (email, auth.hash_password(secrets.token_urlsafe(32)), now_str,
                 trial_ends.isoformat(), now_str, now_str, now_str),
            )
            user_id = cur.lastrowid
            conn.commit()
            log.info(f"New Google Sign-In registration: {email} (user_id={user_id}, auto-verified, "
                     f"{auth.TRIAL_DAYS}-day trial started).")
        else:
            user_id = row["id"]
            if row["is_suspended"]:
                return jsonify({"error": f"This account is suspended. Call {SUPPORT_PHONE_NUMBER} for help."}), 403
            updates = {"last_login_at": now_str, "updated_at": now_str}
            if not row["is_verified"]:
                # A password-flow account that never clicked its verification
                # link -- Google just proved ownership of the SAME email, so
                # it's genuinely verified now too.
                updates["is_verified"] = 1
            conn.execute(
                f"UPDATE users SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?",
                (*updates.values(), user_id),
            )
            conn.commit()
            log.info(f"Google Sign-In login: {email} (user_id={user_id}).")
    finally:
        conn.close()

    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return jsonify({"status": "ok", "redirect": "/"})


@app.route("/logout", methods=["POST"])
@auth.login_required
def logout_page():
    session.clear()
    return redirect("/login")


@app.route("/access-restricted")
@auth.login_required
def access_restricted_page():
    reason = request.args.get("reason", "expired")
    return render_template("access_restricted.html", reason=reason)


def _all_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()]
    finally:
        conn.close()


@app.route("/admin/sysadmin", methods=["GET"])
@auth.roles_required("admin")
def admin_sysadmin_page():
    """Milestone 8 (AI System Administrator): the Operations Dashboard --
    agent health, infrastructure, risk state, supervision state, backup
    state, security alerts, recovery history. Read-only page; the data
    itself comes from /api/sysadmin/overview (polled client-side)."""
    return render_template("sysadmin.html")


@app.route("/api/sysadmin/overview")
@auth.roles_required("admin")
def api_sysadmin_overview():
    return jsonify(sysadmin_api.get_overview())


@app.route("/api/runtime/status")
@auth.roles_required("admin")
def api_runtime_status():
    """Milestone 12, Phase 1: the runtime scheduler's own health payload
    (scheduler_state, cycles_executed, last_cycle_timestamp,
    next_scheduled_cycle, last_cycle_duration_ms, active_jobs,
    runtime_uptime_seconds) -- see agents/runtime/lifecycle.py's own
    docstring. Admin-gated, same as every other operational surface in
    this app (_verify_all_routes_protected() requires it -- there is no
    unauthenticated route option in this codebase).

    Milestone 12, Phase 2 Foundation: also carries a "control" key --
    the active policy, emergency_stop state, and every agent's
    schedulability/mode (agents.runtime.scheduling_control.snapshot())
    -- this route is deliberately NOT duplicated into a second endpoint
    for that; it's the same one canonical status source, extended.

    Milestone 14 post-deployment hardening: also carries a "deployment"
    key -- app version/git commit, process PID, supervisor detection
    (confirms run_forever_vps.sh, not just any parent, owns this
    process), uptime, and the real database path in use (see
    runtime_paths.py's own docstring for why this exists: an earlier
    deployment prompt assumed the wrong DB filename, log location, and
    a pkill pattern broad enough to hit an unrelated service on this
    shared VPS). last_snapshot_ts/snapshot_write_lag_seconds are merged
    in here rather than inside get_deployment_status() itself --
    agents/runtime/lifecycle.py deliberately never imports anything
    from agents.intelligence_history (matching its own documented
    "never imports agents.trading_intelligence directly" boundary), so
    this route -- which already imports both freely -- does that one
    piece of composition instead."""
    status = runtime_lifecycle.get_runtime_status()
    last_ts = intelligence_history_store.last_snapshot_ts()
    status["deployment"]["last_snapshot_ts"] = last_ts
    if last_ts:
        # dt.datetime.now() here, not now_ist() -- every writer of this
        # table (intelligence_alerts_cli.py, _run_intelligence_alerts_
        # auto_cycle()) stamps ts via plain dt.datetime.now().isoformat(),
        # so comparing against that exact same clock source is what
        # keeps this an honest lag, not a spurious one from mixing two
        # different time computations.
        lag = (dt.datetime.now() - dt.datetime.fromisoformat(last_ts)).total_seconds()
        status["deployment"]["snapshot_write_lag_seconds"] = round(lag, 1)
    else:
        status["deployment"]["snapshot_write_lag_seconds"] = None

    # Milestone 15, Phase 3: Runtime Scheduler Observability's own
    # alert-delivery counters -- composed here at the route-handler
    # level for the exact same reason last_snapshot_ts/
    # snapshot_write_lag_seconds are above: agents/runtime/lifecycle.py
    # deliberately never imports agents.intelligence_alerts (matching
    # its own "narrow, already-established dependencies only" boundary
    # -- see agents/intelligence_alerts/dedup_store.py's own docstring
    # history for why that boundary is respected rather than added to).
    status["alerts_sent"] = intelligence_alerts_store.count_delivered_telegram()
    status["alerts_suppressed"] = intelligence_alerts_dedup_store.count_suppressions()
    status["alerts_rate_limited"] = intelligence_alerts_rate_limiter.count_rate_limited()

    # Milestone 20, Phase 6: candle_recorder.py freshness, folded into
    # this canonical status route rather than requiring a second call --
    # GET /api/runtime/candle-freshness carries the full per-timeframe
    # breakdown this "by_symbol" key already is; "summary" is the
    # single most-stale reading across every symbol/timeframe pair that
    # currently HAS a recorded candle, so a caller watching only this
    # top-level field still notices a real staleness problem.
    by_symbol = _candle_freshness_snapshot()
    freshest = None
    stalest_lag = None
    any_stale = False
    for tf_map in by_symbol.values():
        for entry in tf_map.values():
            if entry["candle_lag_seconds"] is None:
                continue
            if stalest_lag is None or entry["candle_lag_seconds"] > stalest_lag:
                stalest_lag = entry["candle_lag_seconds"]
            if freshest is None or entry["last_candle_timestamp"] > freshest:
                freshest = entry["last_candle_timestamp"]
            any_stale = any_stale or entry["stale"]
    status["candle_freshness"] = {
        "summary": {
            "last_candle_timestamp": freshest,
            "candle_lag_seconds": stalest_lag,
            "freshness_status": "STALE" if any_stale else ("OK" if freshest else "NO_DATA"),
        },
        "by_symbol": by_symbol,
    }
    status["websocket_connected"] = any(bool(v) for v in state["symbol_viewers"].values())
    status["active_symbols"] = sorted(sym for sym, viewers in state["symbol_viewers"].items() if viewers)
    return jsonify(status)


_CANDLE_FRESHNESS_STALE_SECONDS = {"NSE": 600, "MCX": 900, "BSE": 600}   # 10 min NSE/BSE, 15 min MCX


def _candle_freshness_snapshot(*, now=None) -> dict:
    """Milestone 20, Phase 6: shared by GET /api/runtime/candle-freshness
    (the full per-symbol/per-timeframe breakdown) and GET /api/runtime/
    status's own "candle_freshness" summary key -- one real computation,
    two views, never two implementations. Reports last_candle_timestamp/
    candle_lag_seconds per (symbol, timeframe), and marks a stream
    "STALE" once its lag exceeds _CANDLE_FRESHNESS_STALE_SECONDS for
    that symbol's own exchange (MCX gets a longer allowance -- its
    background-symbol refresh cadence is the same as NSE's, but a
    thinner tick stream on some contracts can leave a bucket open
    slightly longer)."""
    now = now or dt.datetime.now()
    by_symbol = {}
    for symbol in SYMBOLS:
        exchange = agents_market_session.EXCHANGE_MAP.get(symbol, "NSE")
        threshold = _CANDLE_FRESHNESS_STALE_SECONDS.get(exchange, 600)
        by_symbol[symbol] = {}
        for tf in candle_recorder.TIMEFRAMES_SECONDS:
            last = candle_recorder.last_candle_time(symbol, tf)
            lag = candle_recorder.candle_lag_seconds(symbol, tf, now=now)
            by_symbol[symbol][tf] = {
                "last_candle_timestamp": last.isoformat() if last else None,
                "candle_lag_seconds": lag,
                "source": "live_recorder" if last is not None else "unavailable",
                "stale": (lag is None) or (lag > threshold),
            }
    return by_symbol


@app.route("/api/runtime/candle-freshness")
@auth.roles_required("admin")
def api_runtime_candle_freshness():
    """Milestone 20, Phase 6: health metric for candle_recorder.py's
    in-process 1m/3m/5m candles -- read-only, GET-only, full per-symbol/
    per-timeframe breakdown. See _candle_freshness_snapshot()'s own
    docstring; GET /api/runtime/status carries a compact summary of this
    same data under its own "candle_freshness" key."""
    return jsonify(_candle_freshness_snapshot())


@app.route("/api/runtime/health-snapshot")
@auth.roles_required("admin")
def api_runtime_health_snapshot():
    """Milestone 16, Phase 2: a consolidated operational health view --
    scheduler status/heartbeat/metrics/circuit-breaker state (all
    already in /api/runtime/status's own get_runtime_status()), plus
    the last 20 ops events, an alert-delivery summary (sent/suppressed/
    rate_limited), and how many conditions are currently in an active
    dedup cooldown. Read-only -- agents.ops.diagnostics.
    build_health_snapshot() only ever SELECTs. A genuinely new,
    separate endpoint from /api/runtime/status rather than another
    extension of it, since this one embeds actual event-log rows
    (a meaningfully different, larger payload shape/purpose) --
    matching how /api/shadow/status and /api/shadow/performance already
    stay separate routes for separate purposes in this codebase."""
    return jsonify(ops_diagnostics.build_health_snapshot())


@app.route("/api/runtime/diagnostics.json")
@auth.roles_required("admin")
def api_runtime_diagnostics_json():
    """Milestone 16, Phase 2: a downloadable, fuller diagnostics bundle
    -- runtime status, a compact metrics snapshot, the last 50 ops
    events, every currently-active cooldown fingerprint, circuit-
    breaker state, and a NON-SECRET configuration summary (booleans and
    thresholds only -- see agents.ops.diagnostics's own module
    docstring for the explicit no-raw-credential-value guarantee).
    Read-only. Content-Disposition suggests a filename for the
    "downloadable" framing; the JSON body itself is unaffected."""
    resp = jsonify(ops_diagnostics.build_diagnostics_bundle())
    resp.headers["Content-Disposition"] = "attachment; filename=diagnostics.json"
    return resp


@app.route("/api/ops/events")
@auth.roles_required("admin")
def api_ops_events():
    """Milestone 16, Phase 4: Operational Dashboard APIs. Paginated ops
    event log listing -- read-only, agents.ops.event_log.get_events()
    only ever SELECTs. ?event_type= filters to one type (any value in
    agents.ops.models.ALL_EVENT_TYPES); omitted returns every type."""
    event_type = request.args.get("event_type") or None
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    return jsonify({
        "events": ops_event_log.get_events(limit=limit, offset=offset, event_type=event_type),
        "total": ops_event_log.count_events(event_type=event_type),
        "limit": limit, "offset": offset,
    })


@app.route("/api/ops/metrics/history")
@auth.roles_required("admin")
def api_ops_metrics_history():
    """Milestone 16, Phase 4: a time series of scheduler metrics --
    honestly derived from the already-recorded HEARTBEAT_UPDATED ops
    events (each one IS a metrics snapshot at that point in time,
    throttled to once every HEARTBEAT_LOG_EVERY_N_CYCLES ticks -- see
    agents/runtime/scheduler.py's own tick()), not a separately-tracked
    history this app doesn't otherwise keep."""
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify({
        "metrics_history": ops_event_log.get_events(limit=limit, event_type=ops_models.HEARTBEAT_UPDATED),
    })


@app.route("/api/ops/circuit-breaker")
@auth.roles_required("admin")
def api_ops_circuit_breaker():
    """Milestone 16, Phase 4: just the circuit-breaker slice of
    /api/runtime/status, for a dashboard widget that only needs this
    one piece rather than the full status payload."""
    status = runtime_lifecycle.get_runtime_status()
    return jsonify({
        "state": status.get("circuit_state"),
        "consecutive_failures": status.get("circuit_consecutive_failures"),
    })


@app.route("/api/ops/alerts/summary")
@auth.roles_required("admin")
def api_ops_alerts_summary():
    """Milestone 16, Phase 4: sent/suppressed/deduplicated/rate_limited/
    retried/failed counts -- see agents.ops.diagnostics.
    build_alerts_summary()'s own docstring for why suppressed and
    deduplicated are the same number in this codebase."""
    return jsonify(ops_diagnostics.build_alerts_summary())


def _require_runtime_control_api_enabled():
    """Milestone 12, Phase 2A: every write route below calls this
    first. Returns a (response, status_code) 403 tuple when the new
    action surface is disabled (the default), or None to proceed --
    independent of RUNTIME_SCHEDULER_ENABLED, since this flag governs
    whether the routes DO anything at all, not whether the scheduler is
    running."""
    if not agents_config.RUNTIME_CONTROL_API_ENABLED:
        return jsonify({"error": "runtime control API is disabled by configuration"}), 403
    return None


def _admin_identity() -> str:
    return g.user["username"] or g.user["email"]


@app.route("/api/runtime/control/pause", methods=["POST"])
@auth.roles_required("admin")
def api_runtime_control_pause():
    """Milestone 12, Phase 2A: the global kill switch, reachable from
    the dashboard instead of only runtime_control_cli.py. Calls the
    exact same agents.runtime.policy_engine.set_policy() the CLI's own
    `pause` subcommand calls -- audit trail (sysadmin_report +
    best-effort POLICY_CHANGED event) is already handled inside that
    function; nothing new to log there, only who acted from the web
    session."""
    disabled = _require_runtime_control_api_enabled()
    if disabled:
        return disabled
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    admin = _admin_identity()
    runtime_policy_engine.set_policy(
        runtime_policy_engine.EMERGENCY_STOP, changed_by=admin, reason=reason,
    )
    log.info(f"Admin {admin} paused the runtime (emergency_stop) via /api/runtime/control/pause: {reason}")
    return jsonify({"status": "ok", "active_policy": runtime_policy_engine.get_active_policy()})


@app.route("/api/runtime/control/resume", methods=["POST"])
@auth.roles_required("admin")
def api_runtime_control_resume():
    """Milestone 12, Phase 2A: clears emergency_stop and restores a
    policy (default: agents_config.RUNTIME_DEFAULT_POLICY, matching
    runtime_control_cli.py's own `resume` subcommand default)."""
    disabled = _require_runtime_control_api_enabled()
    if disabled:
        return disabled
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    policy = data.get("policy") or agents_config.RUNTIME_DEFAULT_POLICY
    admin = _admin_identity()
    try:
        runtime_policy_engine.set_policy(policy, changed_by=admin, reason=reason)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    log.info(f"Admin {admin} resumed the runtime (policy={policy!r}) via /api/runtime/control/resume: {reason}")
    return jsonify({"status": "ok", "active_policy": runtime_policy_engine.get_active_policy()})


@app.route("/api/runtime/control/agent/<agent>/mode", methods=["POST"])
@auth.roles_required("admin")
def api_runtime_control_agent_mode(agent):
    """Milestone 12, Phase 2A: per-agent enable/disable/dry-run,
    reachable from the dashboard. Calls agents.runtime.
    scheduling_control.set_mode() unchanged -- that function already
    refuses trading_intelligence/quant_researcher under any mode
    (ValueError), which this route surfaces as a 400, not a 500; no new
    exclusion logic is added here."""
    disabled = _require_runtime_control_api_enabled()
    if disabled:
        return disabled
    data = request.get_json(force=True) or {}
    mode = data.get("mode")
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    admin = _admin_identity()
    try:
        runtime_scheduling_control.set_mode(agent, mode, changed_by=admin, reason=reason)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    log.info(f"Admin {admin} set {agent} schedule_mode={mode!r} via /api/runtime/control/agent/{agent}/mode: {reason}")
    return jsonify({"status": "ok", "agent": agent, "mode": runtime_scheduling_control.get_mode(agent)})


@app.route("/api/runtime/trading-mode")
@auth.roles_required("admin")
def api_runtime_trading_mode():
    """Milestone 14, Phase 3: current PAPER/LIVE_ENABLED/LIVE_DISABLED
    badge state + the last few audit entries -- what the dashboard badge
    polls. See agents/runtime/trading_mode.py's own module docstring:
    LIVE_ENABLED is a label only, never real broker execution."""
    status = runtime_trading_mode.get_status()
    status["history"] = runtime_trading_mode.audit_history(limit=10)
    return jsonify(status)


@app.route("/api/runtime/enable-live", methods=["POST"])
@auth.roles_required("admin")
def api_runtime_enable_live():
    """Milestone 14, Phase 3: explicit manual action required (a
    dashboard button, never automatic) -- `reason` is mandatory, same as
    every other runtime-control route in this file. `acknowledge_no_execution`
    must also be `true`: a small extra safety rail so an admin flipping
    this can't mistake it for turning on real trading -- it can't."""
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    if data.get("acknowledge_no_execution") is not True:
        return jsonify({
            "error": "acknowledge_no_execution must be true -- this confirms you understand LIVE_ENABLED "
                     "is a label only; no code path in this repository can place a real broker order.",
        }), 400
    admin = _admin_identity()
    status = runtime_trading_mode.set_mode(runtime_trading_mode.LIVE_ENABLED, changed_by=admin, reason=reason)
    log.info(f"Admin {admin} set trading mode to LIVE_ENABLED via /api/runtime/enable-live: {reason} "
             f"(label only -- no real broker order capability exists in this codebase).")
    return jsonify(status)


@app.route("/api/runtime/disable-live", methods=["POST"])
@auth.roles_required("admin")
def api_runtime_disable_live():
    """Milestone 14, Phase 3: always available (no acknowledge gate --
    turning OFF never needs a safety rail). Mirrors pause/resume's
    `reason`-required contract."""
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    admin = _admin_identity()
    status = runtime_trading_mode.set_mode(runtime_trading_mode.LIVE_DISABLED, changed_by=admin, reason=reason)
    log.info(f"Admin {admin} set trading mode to LIVE_DISABLED via /api/runtime/disable-live: {reason}")
    return jsonify(status)


@app.route("/api/shadow/status")
@auth.roles_required("admin")
def api_shadow_status():
    """Milestone 12, Phase 2B: Shadow Mode's own health payload --
    observation/prediction counts and the last prediction timestamp.
    GET-only (no methods= argument -- Flask/Werkzeug's default is
    GET/HEAD/OPTIONS, so a POST to this URL 405s automatically, no
    extra code needed). Read-only: agents.shadow_mode.api.get_status()
    only ever SELECTs."""
    return jsonify(shadow_api.get_status())


@app.route("/api/shadow/recent")
@auth.roles_required("admin")
def api_shadow_recent():
    """Last N (default 10) hypothetical predictions, newest first, with
    their outcome if evaluated. GET-only, read-only -- see
    api_shadow_status's own docstring for both guarantees."""
    symbol = request.args.get("symbol") or None
    limit = min(int(request.args.get("limit", 10)), 100)
    return jsonify(shadow_api.get_recent(symbol=symbol, limit=limit))


@app.route("/api/shadow/performance")
@auth.roles_required("admin")
def api_shadow_performance():
    """Rolling Shadow Mode performance metrics (win rate, average move
    captured, confidence calibration). GET-only, read-only -- see
    api_shadow_status's own docstring for both guarantees."""
    symbol = request.args.get("symbol") or None
    since_ts = request.args.get("since_ts") or None
    return jsonify(shadow_api.get_performance(symbol=symbol, since_ts=since_ts))


@app.route("/api/intelligence/snapshot")
@auth.roles_required("admin")
def api_intelligence_snapshot():
    """Milestone 13, Phase 1: Intelligence Orchestrator. GET-only (no
    methods= argument -- Flask/Werkzeug's default is GET/HEAD/OPTIONS,
    so POST/PUT/PATCH/DELETE 405 automatically), read-only --
    intelligence_orchestrator.build_snapshot() only ever reads already-
    stored data, never calls a broker, never writes anywhere. Returns
    404 with an honest reason (not a fabricated snapshot) if no market
    data has been logged yet for the requested symbol."""
    symbol = request.args.get("symbol")
    if not symbol:
        return jsonify({"error": "symbol query parameter is required, e.g. ?symbol=NIFTY"}), 400
    snapshot = intelligence_orchestrator.build_snapshot(symbol)
    if snapshot is None:
        return jsonify({"error": f"no market snapshot available yet for {symbol!r}"}), 404
    return jsonify(snapshot.to_dict())


@app.route("/api/intelligence/history/status")
@auth.roles_required("admin")
def api_intelligence_history_status():
    """Milestone 13, Phase 2: Live Observational Validation. GET-only,
    read-only -- agents.intelligence_history.api.get_status() only ever
    SELECTs. Snapshot logging itself only ever happens via
    intelligence_history_cli.py; no route here ever writes."""
    return jsonify(intelligence_history_api.get_status())


@app.route("/api/intelligence/history/recent")
@auth.roles_required("admin")
def api_intelligence_history_recent():
    """Last N (default 10) logged intelligence snapshots, newest first.
    GET-only, read-only -- see api_intelligence_history_status's own
    docstring for both guarantees."""
    symbol = request.args.get("symbol") or None
    limit = min(int(request.args.get("limit", 10)), 100)
    return jsonify(intelligence_history_api.get_recent(symbol=symbol, limit=limit))


@app.route("/api/intelligence/history/report")
@auth.roles_required("admin")
def api_intelligence_history_report():
    """Drift/stability report (bias stability, confidence stability,
    Greeks coherence, OI responsiveness, bias/price correlation) over
    already-logged history for one symbol. GET-only, read-only -- see
    api_intelligence_history_status's own docstring for both
    guarantees."""
    symbol = request.args.get("symbol")
    if not symbol:
        return jsonify({"error": "symbol query parameter is required, e.g. ?symbol=NIFTY"}), 400
    since_ts = request.args.get("since_ts") or None
    return jsonify(intelligence_history_api.get_report(symbol=symbol, since_ts=since_ts))


@app.route("/api/intelligence/history/page")
@auth.roles_required("admin")
def api_intelligence_history_page():
    """Milestone 13, Phase 3: paginated logged-snapshot listing backing
    the dashboard's read-only history table. GET-only, read-only -- see
    api_intelligence_history_status's own docstring for both
    guarantees."""
    symbol = request.args.get("symbol") or None
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    return jsonify(intelligence_history_api.get_recent_page(symbol=symbol, limit=limit, offset=offset))


@app.route("/api/intelligence/history/snapshot/<int:snapshot_id>")
@auth.roles_required("admin")
def api_intelligence_history_snapshot(snapshot_id):
    """Milestone 13, Phase 3: single logged snapshot in full, backing the
    dashboard's snapshot detail modal. GET-only, read-only -- see
    api_intelligence_history_status's own docstring for both
    guarantees. Returns 404 with an honest reason (not a fabricated
    snapshot) if the id doesn't exist."""
    snapshot = intelligence_history_api.get_snapshot(snapshot_id)
    if snapshot is None:
        return jsonify({"error": f"no logged snapshot with id {snapshot_id}"}), 404
    return jsonify(snapshot)


@app.route("/api/expiry-status")
@auth.subscription_required
def api_expiry_status():
    """Milestone 17+: live, calendar-independent expiry status for every
    configured index -- backs the dashboard's expiry banner. GET-only,
    read-only: expiry_intelligence.get_all_index_expiry_flags() only ever
    reads the already-loaded Angel One instrument master via the shared
    fetcher below, never opens a new broker session or writes anywhere.

    Deliberately reuses _shared_angel_fetcher ONLY -- unlike
    _get_position_monitor_angel(), this never falls back to constructing a
    fresh AngelOneFetcher() (that constructor performs a real broker
    login; see AngelOneFetcher.__init__). If the background symbol loops
    haven't started yet (or SKIP_AUTOSTART=1), degrades to a 503 with an
    honest reason instead of ever risking a second live login."""
    if _shared_angel_fetcher is None:
        return jsonify({"error": "instrument master not loaded yet -- try again shortly"}), 503
    indexes = {s: SYMBOLS[s]["exch"] for s in SYMBOLS if SYMBOLS[s]["type"] == "index_option"}
    flags = expiry_intelligence.get_all_index_expiry_flags(_shared_angel_fetcher, indexes=indexes)
    global_context = expiry_intelligence.global_context_from_flags(flags)
    return jsonify({
        **global_context,
        "indexes": {
            idx: {**status, "next_expiry": status["next_expiry"].isoformat()} if "next_expiry" in status else status
            for idx, status in flags.items()
        },
    })


def _resolve_ti_expiry_dates(symbols):
    """Milestone 17+ audit finding: every real caller of
    agents.trading_intelligence.api.get_overview()/run_scheduled_cycle()
    left expiry_date at its None default, always -- Time Horizon and
    expiry-day signal weighting were silently unavailable for the entire
    Trading Intelligence platform. `symbols` is a mix of NSE indexes and
    MCX commodities with genuinely different expiry calendars, so this
    resolves ONE real date per symbol (never a single shared guess) via
    the same read-only path /api/expiry-status uses -- reuses
    _shared_angel_fetcher only, never opens a new broker session. A
    symbol whose expiry can't be resolved this cycle is simply left out
    of the returned dict (get_overview/run_scheduled_cycle already treat
    a missing entry as "no expiry date available", their pre-existing,
    tested degrade path)."""
    if _shared_angel_fetcher is None:
        return {}
    dates = {}
    for symbol in symbols:
        try:
            dates[symbol] = expiry_intelligence.get_nearest_expiry(symbol, _shared_angel_fetcher)
        except expiry_intelligence.ExpiryDataUnavailable:
            continue
    return dates


@app.route("/api/intelligence/alerts/status")
@auth.roles_required("admin")
def api_intelligence_alerts_status():
    """Milestone 14, Phase 1: Intelligence Alerting Layer. GET-only,
    read-only -- agents.intelligence_alerts.api.get_status() only ever
    SELECTs or reads config constants. Rule evaluation and delivery only
    ever happen via intelligence_alerts_cli.py; no route here ever
    writes or sends anything."""
    return jsonify(intelligence_alerts_api.get_status())


@app.route("/api/intelligence/alerts/recent")
@auth.roles_required("admin")
def api_intelligence_alerts_recent():
    """Paginated logged-alert listing backing the dashboard's alert
    history table. GET-only, read-only -- see
    api_intelligence_alerts_status's own docstring for both
    guarantees."""
    symbol = request.args.get("symbol") or None
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    return jsonify(intelligence_alerts_api.get_recent_page(symbol=symbol, limit=limit, offset=offset))


@app.route("/api/intelligence/alerts/rules")
@auth.roles_required("admin")
def api_intelligence_alerts_rules():
    """Read-only dump of the ACTIVE (effective) threshold config -- an
    override if POST /api/intelligence/alerts/config below has set one,
    else the agents/config.py default. Milestone 14, Phase 3: this
    route itself stayed unchanged in shape/name (still GET-only,
    read-only, never gated) -- what changed is that the values it shows
    can now be genuinely live/operator-set rather than a static file
    dump. GET-only, read-only -- see api_intelligence_alerts_status's
    own docstring for both guarantees."""
    return jsonify(intelligence_alerts_api.get_rules())


@app.route("/api/intelligence/alerts/config", methods=["POST"])
@auth.roles_required("admin")
def api_intelligence_alerts_config():
    """Milestone 14, Phase 3: the one write route that can override an
    alert threshold at runtime instead of editing agents/config.py by
    hand. Gated behind INTELLIGENCE_ALERT_CONFIG_API_ENABLED (off by
    default) -- the read side above is never gated. Body:
    {"key": "confidence_window", "value": 7, "reason": "..."} to set an
    override, or {"key": "confidence_window", "clear": true, "reason": "..."}
    to revert that key to its default. `reason` is required, same
    convention /api/runtime/control/pause|resume and
    /api/trading-intelligence/run-cycle already use. Does NOT make
    INTELLIGENCE_ALERTS_AUTO_ENABLED or TI_RUN_CYCLE_API_ENABLED
    themselves configurable this way -- see agents/config.py's own
    comment on INTELLIGENCE_ALERT_CONFIG_API_ENABLED for why those stay
    .env/restart-gated. Never touches detect_bias()/classify_buildup()/
    generate_signal() or any trading logic -- only the alert-rule
    tuning knobs in agents/intelligence_alerts/."""
    if not agents_config.INTELLIGENCE_ALERT_CONFIG_API_ENABLED:
        return jsonify({"error": "intelligence alert config API is disabled by configuration"}), 403
    data = request.get_json(force=True) or {}
    key = data.get("key")
    if not key:
        return jsonify({"error": "key is required."}), 400
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    admin = _admin_identity()

    try:
        if data.get("clear"):
            result = intelligence_alerts_api.clear_threshold(key, updated_by=admin, reason=reason)
            action = "cleared"
        else:
            if "value" not in data:
                return jsonify({"error": "value is required unless clear is true."}), 400
            result = intelligence_alerts_api.set_threshold(key, data["value"], updated_by=admin, reason=reason)
            action = "set"
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    log.info(f"Admin {admin} {action} intelligence alert threshold {key!r} via "
             f"/api/intelligence/alerts/config (reason: {reason!r})")
    return jsonify({"status": "ok", "rules": result})


@app.route("/api/structure/<symbol>/overlay")
@auth.roles_required("admin")
def api_structure_overlay(symbol):
    """Milestone 20, Phase 5: read-only Structure Overlay panel data --
    GET-only, read-only. structure_overlay.compute_overlay() only ever
    reads already-live OI/candle data through the same
    institutional_levels functions structure_alerts.py's real alert
    path uses; it never sends Telegram, never renders a chart, never
    opens a trade, never writes anywhere."""
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        return jsonify({"error": f"unknown symbol {symbol!r}"}), 404
    return jsonify(structure_overlay.compute_overlay(symbol))


@app.route("/api/papertrades/diagnostics")
@auth.roles_required("admin")
def api_papertrades_diagnostics():
    """Milestone 20, Phase 6: read-only "why did today's paper trades
    win or lose" report -- GET-only, admin-gated. `?date=YYYY-MM-DD`
    (default: today, IST); paper_trade_diagnostics.compute_diagnostics()
    is pure aggregation over ti_paper_trades, never a write, never a
    broker call."""
    date_str = request.args.get("date") or now_ist().date().isoformat()
    try:
        dt.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": f"invalid date {date_str!r} -- expected YYYY-MM-DD"}), 400
    return jsonify(paper_trade_diagnostics.compute_diagnostics(date_str))


@app.route("/api/structure/tuning/history")
@auth.roles_required("admin")
def api_structure_tuning_history():
    """Milestone 20, Phase 7: read-only audit trail for the bounded/
    rate-limited adaptive structure-tuning loop -- GET-only, admin-
    gated. Every evaluation this loop has ever run, applied or not,
    with the full backtest evidence (current/best win rates, sample
    size, reason) behind each decision. `?parameter=` filters to one
    tunable parameter; `?limit=` caps rows (default 50)."""
    parameter = request.args.get("parameter")
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({
        "current_values": {name: getattr(institutional_levels, spec["attr"])
                            for name, spec in structure_tuning.TUNABLE_PARAMS.items()},
        "history": structure_tuning.list_tuning_history(parameter=parameter, limit=limit),
    })


@app.route("/api/papertrades/virtual-trailing")
@auth.roles_required("admin")
def api_papertrades_virtual_trailing():
    """Milestone 21, Phase 1: read-only current state of the Virtual
    Trailing Engine -- GET-only, admin-gated. Paper-trade / advisory
    only; this route (and everything under it) never writes and never
    touches a broker. `?symbol=` optional filter; `?active_only=1` to
    exclude trades that have already virtually exited."""
    symbol = request.args.get("symbol")
    active_only = request.args.get("active_only") in ("1", "true", "yes")
    return jsonify({"trades": virtual_trailing.list_states(symbol=symbol, active_only=active_only)})


@app.route("/api/monitoring/control-center")
@auth.roles_required("admin")
def api_monitoring_control_center():
    """Milestone 21, Phase 2: the Autonomous Trade Control Center's full
    payload -- GET-only, admin-gated, feature-flagged
    (config.TI_ENABLE_CONTROL_CENTER_UI). Pure aggregation over already-
    stored data (see monitoring_center.py's own docstring); never writes,
    never touches a broker. `?symbol=` scopes the AI Bias/Confidence/
    Institutional Score card only."""
    if not agents_config.TI_ENABLE_CONTROL_CENTER_UI:
        return jsonify({"error": "control center is disabled -- set TI_ENABLE_CONTROL_CENTER_UI=true"}), 404
    return jsonify(monitoring_center.get_control_center_snapshot(symbol=request.args.get("symbol")))


@app.route("/api/monitoring/health")
@auth.roles_required("admin")
def api_monitoring_health():
    """Milestone 21, Phase 2: the trading_intelligence scheduler agent's
    own execution bookkeeping (heartbeat, last run, health score) --
    GET-only, admin-gated, feature-flagged. Same underlying read
    monitoring_center's "Scheduler Health" card uses."""
    if not agents_config.TI_ENABLE_CONTROL_CENTER_UI:
        return jsonify({"error": "control center is disabled -- set TI_ENABLE_CONTROL_CENTER_UI=true"}), 404
    status = agent_sysadmin_store.get_agent_status("trading_intelligence")
    return jsonify({"available": status is not None, "status": status})


@app.route("/api/monitoring/control-center/pause", methods=["POST"])
@auth.roles_required("admin")
def api_monitoring_pause():
    """Pauses ONLY the Virtual Trailing Engine's own per-cycle state
    updates (virtual_trailing.run_virtual_trailing_cycle()) -- the real
    paper-trading recommendation engine is completely unaffected. Never
    touches a broker or a real trade."""
    if not agents_config.TI_ENABLE_CONTROL_CENTER_UI:
        return jsonify({"error": "control center is disabled -- set TI_ENABLE_CONTROL_CENTER_UI=true"}), 404
    monitoring_center.pause_monitoring()
    return jsonify({"paused": True})


@app.route("/api/monitoring/control-center/resume", methods=["POST"])
@auth.roles_required("admin")
def api_monitoring_resume():
    if not agents_config.TI_ENABLE_CONTROL_CENTER_UI:
        return jsonify({"error": "control center is disabled -- set TI_ENABLE_CONTROL_CENTER_UI=true"}), 404
    monitoring_center.resume_monitoring()
    return jsonify({"paused": False})


@app.route("/api/monitoring/control-center/reset-virtual-state", methods=["POST"])
@auth.roles_required("admin")
def api_monitoring_reset_virtual_state():
    """Deletes ONE trade's virtual_trailing_state row (advisory table
    only -- never ti_paper_trades, never a broker). The next cycle
    re-initializes it from scratch off the real trade's own current
    entry/SL/target. Requires `?trade_id=` (or a JSON body
    `{"trade_id": ...}`)."""
    if not agents_config.TI_ENABLE_CONTROL_CENTER_UI:
        return jsonify({"error": "control center is disabled -- set TI_ENABLE_CONTROL_CENTER_UI=true"}), 404
    trade_id = request.args.get("trade_id", type=int)
    if trade_id is None and request.is_json:
        trade_id = (request.get_json(silent=True) or {}).get("trade_id")
    if trade_id is None:
        return jsonify({"error": "trade_id is required"}), 400
    removed = monitoring_center.reset_trade(int(trade_id))
    return jsonify({"reset": removed})


@app.route("/api/trading-intelligence/run-cycle", methods=["POST"])
@auth.roles_required("admin")
def api_trading_intelligence_run_cycle():
    """Today Signal Audit follow-up: web-triggered equivalent of
    `trading_intelligence_cli.py run-cycle` -- calls the exact same
    agents.trading_intelligence.api.run_scheduled_cycle(), the ONLY
    function that ever opens a ti_paper_trades row (see that CLI's own
    module docstring for the full root-cause trace). Gated behind
    TI_RUN_CYCLE_API_ENABLED (off by default); does not touch
    RUNTIME_SCHEDULER_ENABLED or scheduling_control.NEVER_SCHEDULABLE_AGENTS
    -- this stays a human-clicks-a-button, one-shot trigger, never a
    recurring one. `reason` is required, same convention
    /api/runtime/control/pause|resume already use, so every trigger has
    an auditable why attached to who did it."""
    if not agents_config.TI_RUN_CYCLE_API_ENABLED:
        return jsonify({"error": "trading intelligence run-cycle API is disabled by configuration"}), 403
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    admin = _admin_identity()
    results = ti_api.run_scheduled_cycle(expiry_dates=_resolve_ti_expiry_dates(agents_config.TI_WATCHED_SYMBOLS))
    trades_opened = [s for s, r in results.items() if r.get("trade_opened")]
    log.info(f"Admin {admin} ran a Trading Intelligence cycle via /api/trading-intelligence/run-cycle "
             f"(reason: {reason!r}) -- trades opened: {trades_opened or 'none'}")
    return jsonify({"status": "ok", "results": results})


@app.route("/admin/trading-intelligence", methods=["GET"])
@auth.roles_required("admin")
def admin_trading_intelligence_page():
    """Milestone 10 (BATI Trading Intelligence Platform): live option
    chain, OI analytics, Greeks, AI signals, risk/confidence, paper P&L,
    agent health. Read-only page; recommendation mode and paper trading
    only -- see agents/trading_intelligence/__init__.py's own safety
    rule. Data itself comes from /api/trading-intelligence/overview
    (polled client-side)."""
    return render_template(
        "trading_intelligence.html",
        control_center_enabled=agents_config.TI_ENABLE_CONTROL_CENTER_UI,
        ai_live_snapshot_enabled=False,
    )


@app.route("/api/trading-intelligence/overview")
@auth.roles_required("admin")
def api_trading_intelligence_overview():
    return jsonify(ti_api.get_overview(expiry_dates=_resolve_ti_expiry_dates(agents_config.TI_WATCHED_SYMBOLS)))


@app.route("/admin/users", methods=["GET"])
@auth.roles_required("admin")
def admin_users_page():
    return render_template("admin_users.html", users=_all_users(), revealed_link=None, revealed_for=None)


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@auth.roles_required("admin")
def admin_set_role(user_id):
    role = request.form.get("role")
    if role not in ("admin", "developer", "subscriber"):
        abort(400)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, now_ist().isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()
    log.info(f"Admin {g.user['username'] or g.user['email']} set user_id={user_id} role -> {role}")
    return redirect("/admin/users")


@app.route("/admin/users/<int:user_id>/subscription", methods=["POST"])
@auth.roles_required("admin")
def admin_set_subscription(user_id):
    plan = request.form.get("plan") or None
    expires_at_raw = request.form.get("expires_at") or None
    if plan not in (None, "monthly", "quarterly", "yearly"):
        abort(400)
    expires_at_iso = None
    if expires_at_raw:
        try:
            expires_at_iso = dt.datetime.strptime(expires_at_raw, "%Y-%m-%d").isoformat()
        except ValueError:
            abort(400)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE users SET subscription_plan=?, subscription_expires_at=?, updated_at=? WHERE id=?",
            (plan, expires_at_iso, now_ist().isoformat(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(f"Admin {g.user['username'] or g.user['email']} set user_id={user_id} subscription -> {plan} until {expires_at_iso}")
    return redirect("/admin/users")


@app.route("/admin/users/<int:user_id>/wallet", methods=["POST"])
@auth.roles_required("admin")
def admin_adjust_wallet(user_id):
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        abort(400)
    note = (request.form.get("note") or "").strip() or None
    reason = "admin_topup" if amount >= 0 else "admin_adjustment"
    try:
        new_balance = billing.create_wallet_transaction(
            user_id, amount, reason, note=note, created_by_user_id=g.user["id"]
        )
    except ValueError:
        abort(404)
    log.info(f"Admin {g.user['username'] or g.user['email']} adjusted user_id={user_id} wallet by {amount:+.2f} -> {new_balance:.2f}")
    return redirect("/admin/users")


@app.route("/admin/users/<int:user_id>/suspend", methods=["POST"])
@auth.roles_required("admin")
def admin_toggle_suspend(user_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT is_suspended FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            abort(404)
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE users SET is_suspended=?, updated_at=? WHERE id=?", (new_val, now_ist().isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()
    log.info(f"Admin {g.user['username'] or g.user['email']} {'suspended' if new_val else 'unsuspended'} user_id={user_id}")
    return redirect("/admin/users")


@app.route("/admin/users/<int:user_id>/verify", methods=["POST"])
@auth.roles_required("admin")
def admin_manual_verify(user_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT is_verified, trial_started_at FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            abort(404)
        now_str = now_ist().isoformat()
        if row[1]:   # trial already started once -- don't reset it on a re-verify
            conn.execute("UPDATE users SET is_verified=1, updated_at=? WHERE id=?", (now_str, user_id))
        else:
            trial_ends = now_ist() + dt.timedelta(days=auth.TRIAL_DAYS)
            conn.execute(
                "UPDATE users SET is_verified=1, trial_started_at=?, trial_ends_at=?, updated_at=? WHERE id=?",
                (now_str, trial_ends.isoformat(), now_str, user_id),
            )
        conn.commit()
    finally:
        conn.close()
    log.info(f"Admin {g.user['username'] or g.user['email']} manually verified user_id={user_id}")
    return redirect("/admin/users")


@app.route("/admin/users/<int:user_id>/resend-verification", methods=["POST"])
@auth.roles_required("admin")
def admin_resend_verification(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            abort(404)
        raw_token, token_hash = auth.generate_verification_token()
        conn.execute(
            "UPDATE users SET verification_token_hash=?, verification_token_expires_at=?, updated_at=? WHERE id=?",
            (token_hash, auth.token_expiry().isoformat(), now_ist().isoformat(), user_id),
        )
        conn.commit()
        emailed = False
        if row["email"]:
            emailed = auth.send_email(
                row["email"], "Verify your account",
                auth.verification_email_body(raw_token, request.host_url),
            )
        link = f"{request.host_url.rstrip('/')}/verify-email/{raw_token}"
        log.info(f"Admin {g.user['username'] or g.user['email']} regenerated verification link for user_id={user_id} (emailed={emailed})")
        # Rendered directly (not redirected) so the raw link -- which cannot ever
        # be recovered again once this response is gone -- only ever appears in
        # this one authenticated admin response, never in a URL/query string,
        # never logged, never stored.
        return render_template("admin_users.html", users=_all_users(), revealed_link=link, revealed_for=row["email"] or row["username"])
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------------

@app.route("/")
@auth.subscription_required
def index():
    groups = {}
    for sym, cfg in SYMBOLS.items():
        groups.setdefault(cfg["group"], []).append({"symbol": sym, "label": cfg["label"]})
    return render_template(
        "dashboard.html",
        symbol_groups=groups,
        unavailable=UNAVAILABLE_SYMBOLS,
        default_symbol=DEFAULT_SYMBOL,
        refresh=REFRESH_INTERVAL,
    )


@app.route("/dev-settings", methods=["GET", "POST"])
@auth.roles_required("admin", "developer")
def dev_settings_page():
    if request.method == "POST":
        state["dev_settings"]["DEV_MODE_WHEN_CLOSED"] = "dev_mode_when_closed" in request.form
        state["dev_settings"]["PAPER_TRADING_ENABLED"] = "paper_trading_enabled" in request.form
        state["dev_settings"]["TRAILING_SL_ENABLED"] = "trailing_sl_enabled" in request.form
        state["dev_settings"]["STAGNANT_EXIT_ENABLED"] = "stagnant_exit_enabled" in request.form
        try:
            state["dev_settings"]["SIGNAL_CONFIDENCE_THRESHOLD"] = int(request.form.get("signal_confidence_threshold", 60))
        except ValueError:
            pass
        try:
            state["dev_settings"]["VOLUME_EXPANSION_MULT"] = float(request.form.get("volume_expansion_mult", 1.5))
        except ValueError:
            pass
        for symbol in SYMBOLS.keys():
            state["engine_v2_enabled"][symbol] = f"v2_enabled_{symbol}" in request.form
            state["v3_engine_enabled"][symbol] = f"v3_enabled_{symbol}" in request.form
        state["new_trend_meter_enabled"] = "new_trend_meter_enabled" in request.form
        state["scalp_engine_enabled"] = "scalp_engine_enabled" in request.form
        state["ichimoku_engine_enabled"] = "ichimoku_engine_enabled" in request.form
        state["manual_trade_delete_enabled"] = "manual_trade_delete_enabled" in request.form
        log.info(f"Dev settings updated live (no restart): {state['dev_settings']}, "
                 f"Engine V2 enabled for: {[s for s, v in state['engine_v2_enabled'].items() if v]}, "
                 f"S/R Engine V3 enabled for: {[s for s, v in state['v3_engine_enabled'].items() if v]}, "
                 f"New Trend Meter enabled: {state['new_trend_meter_enabled']}, "
                 f"Scalping Engine enabled: {state['scalp_engine_enabled']}, "
                 f"Ichimoku Engine enabled: {state['ichimoku_engine_enabled']}, "
                 f"Manual-trade delete enabled: {state['manual_trade_delete_enabled']}")
    return render_template("dev_settings.html", settings=state["dev_settings"],
                            symbols=list(SYMBOLS.keys()), v2_enabled=state["engine_v2_enabled"],
                            v3_enabled=state["v3_engine_enabled"],
                            new_trend_meter_enabled=state["new_trend_meter_enabled"],
                            scalp_engine_enabled=state["scalp_engine_enabled"],
                            ichimoku_engine_enabled=state["ichimoku_engine_enabled"],
                            scalp_max_hold_minutes=SCALP_MAX_HOLD_MINUTES,
                            manual_trade_delete_enabled=state["manual_trade_delete_enabled"])


def _run_backtest_job(form, my_token):
    """
    Runs in a background thread (via socketio.start_background_task) so the
    HTTP request that triggered it can return immediately -- the browser
    polls /api/backtest_progress for live updates instead of blocking on a
    single long request with zero feedback.

    my_token: guards against a race condition where two jobs briefly overlap
    (e.g. a stuck-job auto-recovery starting a new job while an old thread
    is still finishing up) -- every write below only applies if this thread's
    token still matches the CURRENT job token, so a stale thread's updates
    can never corrupt the live display with random-looking progress jumps.
    """
    from backtest import (
        simulate_trades, simulate_sr_engine_trades, analyze_v2_signals,
        simulate_v2_engine_trades, simulate_v3_engine_trades, compute_advanced_trade_stats,
        simulate_dynamic_sr_engine_trades, simulate_dynamic_sr_v4_trades,
        simulate_ichimoku_trades, compute_ichimoku_accuracy_stats, ICHIMOKU_MAX_HOLD_MINUTES,
    )
    job = state["backtest_job"]
    try:
        date_to = form["date_to"] or form["date_from"]
        # profile_params: JSON blob of ENGINE_PARAM_SPECS-keyed overrides,
        # populated client-side either from a loaded profile or from
        # whatever's currently in the tunable-parameter fields. Empty/absent
        # means "use every engine's stock defaults" -- unchanged behavior.
        try:
            overrides = json.loads(form.get("profile_params") or "{}")
        except json.JSONDecodeError:
            overrides = {}

        def progress_cb(done, total, elapsed, trades_so_far):
            if job["token"] != my_token:
                return   # a newer job has since started -- this thread is stale, ignore
            job["done"], job["total"], job["elapsed"], job["trades_so_far"] = done, total, round(elapsed, 1), trades_so_far

        if form["engine"] == "sr":
            # proximity_atr_mult/breakeven_trigger_pct/trail_trigger_pct/
            # trail_giveback_pct/cooldown_minutes_after_sl/
            # cooldown_minutes_after_timeout are LIVE-ONLY knobs (see
            # get_sr_live_params) -- the backtest replay has no trailing-stop
            # simulation to apply them to, so they're deliberately not
            # forwarded here even when present in a loaded profile.
            sr_kwargs = {k: overrides[k] for k in
                         ("premium_ema_fast", "premium_ema_slow", "target_delta_approx",
                          "min_target_pct", "max_sl_pct", "max_hold_minutes") if k in overrides}
            trades, cycle_count, meta = simulate_sr_engine_trades(
                form["symbol"], form["date_from"], date_to,
                persistence_seconds=BIAS_PERSISTENCE_SECONDS, deactivation_grace_seconds=NEUTRAL_GRACE_SECONDS,
                min_risk_reward=overrides.get("min_risk_reward", float(form["min_rr"])),
                cooldown_minutes=overrides.get("sr_cooldown_minutes", int(form.get("sr_cooldown", 0))),
                progress_callback=progress_cb, **sr_kwargs,
            )
        elif form["engine"] == "v2":
            v2_result = analyze_v2_signals(form["symbol"], form["date_from"], date_to)
            if job["token"] != my_token:
                return
            if "error" in v2_result:
                job["error"] = v2_result["error"]
                job["running"] = False
                return
            # ADDITIONALLY run a real trade-simulation backtest (added 2026-08-02
            # per request) so Engine V2 can be compared against V1/V3 using the
            # SAME advanced-stats format -- never replaces the existing
            # frequency/directional-accuracy analysis above, shown alongside it.
            v2_step = SYMBOLS.get(form["symbol"], {}).get("step") or 50
            v2_trades, _v2_cycle_count, v2_meta = simulate_v2_engine_trades(
                form["symbol"], form["date_from"], date_to, strike_step=v2_step, progress_callback=progress_cb,
            )
            if job["token"] != my_token:
                return
            job["result"] = {
                "engine": "v2", "v2_analysis": v2_result,
                "trades": [
                    {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                    for t in v2_trades
                ],
                "meta": v2_meta,
                "advanced_stats": compute_advanced_trade_stats(v2_trades),
            }
            job["running"] = False
            return
        elif form["engine"] == "v3":
            v3_kwargs = {k: overrides[k] for k in ("max_hold_minutes",) if k in overrides}
            if "cooldown_minutes_after_sl" in overrides:
                v3_kwargs["cooldown_minutes"] = overrides["cooldown_minutes_after_sl"]
            trades, cycle_count, meta = simulate_v3_engine_trades(
                form["symbol"], form["date_from"], date_to,
                strike_step=SYMBOLS.get(form["symbol"], {}).get("step") or 50,
                progress_callback=progress_cb,
                min_risk_reward=overrides.get("min_risk_reward"),
                confidence_threshold=overrides.get("confidence_threshold"),
                min_target_pct=overrides.get("min_target_pct"), max_sl_pct=overrides.get("max_sl_pct"),
                **v3_kwargs,
            )
            if job["token"] != my_token:
                return
            if meta.get("error"):
                job["error"] = meta["error"]
                job["running"] = False
                return
            job["result"] = {
                "engine": "v3", "cycle_count": cycle_count, "meta": meta,
                "trades": [
                    {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                    for t in trades
                ],
                "advanced_stats": compute_advanced_trade_stats(trades),
            }
            job["running"] = False
            return
        elif form["engine"] == "ichimoku":
            trades, candle_count, meta = simulate_ichimoku_trades(
                form["symbol"], form["date_from"], date_to,
                max_hold_minutes=overrides.get("max_hold_minutes", ICHIMOKU_MAX_HOLD_MINUTES),
                cooldown_minutes=overrides.get("cooldown_minutes_after_sl", 0),
                progress_callback=progress_cb,
            )
            if job["token"] != my_token:
                return
            if meta.get("error"):
                job["error"] = meta["error"]
                job["running"] = False
                return
            job["result"] = {
                "engine": "ichimoku", "candle_count": candle_count, "meta": meta,
                "trades": [
                    {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                    for t in trades
                ],
                "advanced_stats": compute_ichimoku_accuracy_stats(trades),
            }
            job["running"] = False
            return
        elif form["engine"] == "dynamic-sr":
            v1_kwargs = {k: overrides[k] for k in ("max_hold_minutes",) if k in overrides}
            trades, cycle_count, meta = simulate_dynamic_sr_engine_trades(
                form["symbol"], form["date_from"], date_to, progress_callback=progress_cb,
                min_tradeable_confidence=overrides.get("min_tradeable_confidence"), **v1_kwargs,
            )
            if job["token"] != my_token:
                return
            job["result"] = {
                "engine": "dynamic-sr", "cycle_count": cycle_count, "meta": meta,
                "trades": [
                    {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                    for t in trades
                ],
                "advanced_stats": compute_advanced_trade_stats(trades),
            }
            job["running"] = False
            return
        elif form["engine"] == "dynamic-sr-v4":
            trades, cycle_count, meta = simulate_dynamic_sr_v4_trades(
                form["symbol"], form["date_from"], date_to, progress_callback=progress_cb,
                atr_trail_mult=overrides.get("atr_trail_mult"),
                momentum_fade_threshold=overrides.get("momentum_fade_threshold"),
                adaptive_hold_base_minutes=overrides.get("adaptive_hold_base_minutes"),
                adaptive_hold_max_minutes=overrides.get("adaptive_hold_max_minutes"),
                max_sl_atr_mult=overrides.get("max_sl_atr_mult"),
            )
            if job["token"] != my_token:
                return
            job["result"] = {
                "engine": "dynamic-sr-v4", "cycle_count": cycle_count, "meta": meta,
                "trades": [
                    {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                    for t in trades
                ],
                "advanced_stats": compute_advanced_trade_stats(trades),
            }
            job["running"] = False
            return
        else:
            trades, cycle_count = simulate_trades(
                form["symbol"], form["date_from"], date_to,
                overrides.get("persistence_cycles", int(form["persistence"])),
                overrides.get("cooldown_minutes", int(form["cooldown"])),
                overrides.get("confidence_threshold", int(form["confidence"])),
            )
            meta = None

        if job["token"] != my_token:
            return   # stale -- a newer job has already taken over, don't overwrite its result

        wins = [t for t in trades if t["exit_reason"] == "TARGET HIT"]
        losses = [t for t in trades if t["exit_reason"] == "STOP LOSS"]
        time_exits = [t for t in trades if t["exit_reason"] in ("TIME EXIT", "STAGNANT EXIT")]
        total_points = sum(t["points"] for t in trades)
        job["result"] = {
            "cycle_count": cycle_count, "engine": form["engine"], "meta": meta,
            "trades": [
                {**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                 "exit_time": t["exit_time"].strftime("%H:%M:%S")}
                for t in trades
            ],
            "wins": len(wins), "losses": len(losses), "time_exits": len(time_exits),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_points": round(total_points, 2),
        }
    except Exception as e:
        log.warning(f"Backtest job failed: {e}")
        if job["token"] == my_token:
            job["error"] = str(e)
    finally:
        if job["token"] == my_token:
            job["running"] = False


@app.route("/backtest", methods=["GET", "POST"])
@auth.roles_required("admin", "developer")
def backtest_page():
    from backtest import list_available_data_web

    available = list_available_data_web(DB_PATH)
    job = state["backtest_job"]

    if request.method == "POST" and request.form.get("date_from"):
        job_is_genuinely_stuck = (
            job["running"] and job["started_at"]
            and (dt.datetime.now() - job["started_at"]).total_seconds() > 300
        )
        if job["running"] and not job_is_genuinely_stuck:
            pass   # a job is already running -- ignore this submission, just show current progress
        else:
            if job_is_genuinely_stuck:
                log.warning("Backtest job was stuck (running >5min) -- auto-recovering and starting fresh.")
            form = {
                "symbol": request.form.get("symbol", DEFAULT_SYMBOL),
                "date_from": request.form.get("date_from", ""),
                "date_to": request.form.get("date_to", ""),
                "engine": request.form.get("engine", "sr"),
                "persistence": request.form.get("persistence", str(BIAS_PERSISTENCE_SECONDS)),
                "cooldown": request.form.get("cooldown", str(COOLDOWN_MINUTES_AFTER_SL)),
                "confidence": request.form.get("confidence", str(SIGNAL_CONFIDENCE_THRESHOLD)),
                "min_rr": request.form.get("min_rr", str(SR_MIN_RISK_REWARD)),
                "sr_cooldown": request.form.get("sr_cooldown", "0"),
                "profile_params": request.form.get("profile_params", ""),
            }
            new_token = job["token"] + 1
            job.update({"running": True, "done": 0, "total": 0, "elapsed": 0, "trades_so_far": 0,
                        "result": None, "form": form, "error": None, "started_at": dt.datetime.now(), "token": new_token})
            socketio.start_background_task(_run_backtest_job, form, new_token)

        # POST-Redirect-GET: always redirect after a POST, never render
        # directly in the POST response. This guarantees the browser's
        # current page was loaded via GET, so no reload/navigation can ever
        # accidentally resubmit the form (the root cause of the infinite
        # backtest-restart loop found on 2026-07-23).
        return redirect(url_for("backtest_page"))

    form = job["form"] or {
        "symbol": DEFAULT_SYMBOL, "date_from": "", "date_to": "", "engine": "sr",
        "persistence": str(BIAS_PERSISTENCE_SECONDS), "cooldown": str(COOLDOWN_MINUTES_AFTER_SL),
        "confidence": str(SIGNAL_CONFIDENCE_THRESHOLD), "min_rr": str(SR_MIN_RISK_REWARD), "sr_cooldown": "0",
        "profile_params": "",
    }
    results = job["result"] if not job["running"] else None

    engine_param_specs_json = json.dumps({
        engine: {key: {"default": meta["default"], "min": meta["min"], "max": meta["max"],
                        "type": "int" if meta["type"] is int else "float",
                        "step": meta.get("step"), "nullable": bool(meta.get("nullable")),
                        "backtest_only": bool(meta.get("backtest_only"))}
                 for key, meta in spec.items()}
        for engine, spec in ENGINE_PARAM_SPECS.items()
    })
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        profile_rows = conn.execute(
            "SELECT id, symbol, engine, profile_name, params_json, last_backtest_summary_json, is_active_live "
            "FROM backtest_profiles WHERE symbol=? ORDER BY engine, profile_name",
            (form["symbol"],),
        ).fetchall()
    finally:
        conn.close()
    profiles_by_engine = {}
    for r in profile_rows:
        profiles_by_engine.setdefault(r["engine"], []).append({
            "id": r["id"], "profile_name": r["profile_name"],
            "params": json.loads(r["params_json"]),
            "last_backtest_summary": json.loads(r["last_backtest_summary_json"]) if r["last_backtest_summary_json"] else None,
            "is_active_live": bool(r["is_active_live"]),
        })

    return render_template("backtest.html", symbols=list(SYMBOLS.keys()), available=available,
                            form=form, results=results, job=job,
                            engine_param_specs=ENGINE_PARAM_SPECS, engine_param_specs_json=engine_param_specs_json,
                            profiles_by_engine=profiles_by_engine)


@app.route("/api/backtest_progress")
@auth.roles_required("admin", "developer")
def backtest_progress_api():
    """Polled by the backtest page's JS every ~1s while a job is running."""
    job = state["backtest_job"]
    resp = jsonify({
        "running": job["running"], "done": job["done"], "total": job["total"],
        "elapsed": job["elapsed"], "trades_so_far": job["trades_so_far"],
        "has_result": job["result"] is not None, "error": job["error"],
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _validate_profile_params(engine, raw_params):
    """Whitelists raw_params against ENGINE_PARAM_SPECS[engine] -- unknown
    keys are silently dropped (never stored), known keys are type-coerced and
    range-clamped. Returns (clean_params, errors) -- errors is a list of
    human-readable strings for any value that couldn't be coerced at all
    (clamping doesn't error, it just silently bounds).

    nullable params (meta.get("nullable")): unlike every other key, "not
    stored" (key absent -- never configured, e.g. a profile saved before this
    knob existed) and "explicitly disabled" (key present, value null/blank --
    the UI unchecked its enable checkbox or cleared the field) are two
    DIFFERENT states that must round-trip through save/load distinctly, so
    the field's own on-screen checkbox state can be restored correctly --
    not just the number. So a nullable key sent as None/'' is stored as
    None (not dropped): both cases still resolve to "no override" wherever
    the saved params are actually applied (dict.get(key) is None either
    way), this only affects what the UI shows when the profile is reloaded."""
    spec = ENGINE_PARAM_SPECS.get(engine, {})
    clean, errors = {}, []
    for key, meta in spec.items():
        if key not in raw_params:
            continue
        if meta.get("nullable") and raw_params[key] in (None, ""):
            clean[key] = None
            continue
        try:
            val = meta["type"](raw_params[key])
        except (TypeError, ValueError):
            errors.append(f"{key}: invalid value {raw_params[key]!r}, expected {meta['type'].__name__}")
            continue
        val = max(meta["min"], min(meta["max"], val))
        clean[key] = val
    return clean, errors


@app.route("/backtest/profile/save", methods=["POST"])
@auth.roles_required("admin", "developer")
def backtest_profile_save():
    engine = request.form.get("engine", "")
    symbol = request.form.get("symbol", "")
    profile_name = (request.form.get("profile_name") or "").strip()
    if engine not in ENGINE_PARAM_SPECS or not symbol or not profile_name:
        return jsonify({"error": "engine, symbol, and profile_name are all required (engine must have tunable params -- 'v2' does not)."}), 400

    try:
        raw_params = json.loads(request.form.get("params_json") or "{}")
    except json.JSONDecodeError:
        return jsonify({"error": "params_json was not valid JSON."}), 400
    clean_params, errors = _validate_profile_params(engine, raw_params)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    job = state["backtest_job"]
    summary_json = None
    if (job["result"] and job["form"] and job["form"].get("engine") == engine
            and job["form"].get("symbol") == symbol and job["result"].get("advanced_stats")):
        summary_json = json.dumps(job["result"]["advanced_stats"])

    now_str = now_ist().isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO backtest_profiles
                   (user_id, symbol, engine, profile_name, params_json, last_backtest_summary_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, engine, profile_name) DO UPDATE SET
                   params_json=excluded.params_json,
                   last_backtest_summary_json=excluded.last_backtest_summary_json,
                   updated_at=excluded.updated_at""",
            (g.user["id"], symbol, engine, profile_name, json.dumps(clean_params), summary_json, now_str, now_str),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(f"BACKTEST PROFILE SAVED | {symbol}/{engine}/{profile_name} by user_id={g.user['id']}")
    return redirect(url_for("backtest_page"))


@app.route("/api/backtest_profiles")
@auth.roles_required("admin", "developer")
def api_backtest_profiles():
    symbol = request.args.get("symbol", "")
    engine = request.args.get("engine", "")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, engine, profile_name, params_json, last_backtest_summary_json, "
            "is_active_live, created_at, updated_at FROM backtest_profiles "
            "WHERE symbol=? AND engine=? ORDER BY profile_name",
            (symbol, engine),
        ).fetchall()
    finally:
        conn.close()
    return jsonify([{
        "id": r["id"], "symbol": r["symbol"], "engine": r["engine"], "profile_name": r["profile_name"],
        "params": json.loads(r["params_json"]),
        "last_backtest_summary": json.loads(r["last_backtest_summary_json"]) if r["last_backtest_summary_json"] else None,
        "is_active_live": bool(r["is_active_live"]), "created_at": r["created_at"], "updated_at": r["updated_at"],
    } for r in rows])


@app.route("/backtest/profile/load", methods=["POST"])
@auth.roles_required("admin", "developer")
def backtest_profile_load():
    try:
        profile_id = int(request.form.get("profile_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id required"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM backtest_profiles WHERE id=?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Profile not found."}), 404

    # Stash straight into the job's remembered form -- the SAME single
    # source of truth backtest_page's GET branch already reads from
    # (job["form"]) -- so a redirect there pre-fills the form exactly like
    # re-submitting it would, no separate "pending prefill" state needed.
    job = state["backtest_job"]
    params = json.loads(row["params_json"])
    new_form = dict(job["form"] or {})
    new_form.update({
        "symbol": row["symbol"], "engine": row["engine"],
        "profile_params": json.dumps(params), "loaded_profile_name": row["profile_name"],
    })
    job["form"] = new_form
    return redirect(url_for("backtest_page"))


@app.route("/backtest/profile/delete", methods=["POST"])
@auth.roles_required("admin", "developer")
def backtest_profile_delete():
    try:
        profile_id = int(request.form.get("profile_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id required"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT symbol, engine, is_active_live FROM backtest_profiles WHERE id=?", (profile_id,)).fetchone()
        conn.execute("DELETE FROM backtest_profiles WHERE id=?", (profile_id,))
        conn.commit()
    finally:
        conn.close()
    if row and row["is_active_live"]:
        state.get("sr_active_profile_cache", {}).pop(row["symbol"], None)
        log.warning(f"BACKTEST PROFILE DELETED WHILE ACTIVE-LIVE | {row['symbol']}/{row['engine']} -- live now falls back to global defaults.")
    return redirect(url_for("backtest_page"))


@app.route("/backtest/profile/activate", methods=["POST"])
@auth.roles_required("admin", "developer")
def backtest_profile_activate():
    """Marks one 'sr' profile as the live parameter source for its symbol.
    ONLY engine='sr' may be activated -- V1/V3/V4 have no live trading path
    to wire into (see plan). This is the one genuinely consequential action
    in this whole feature: it changes real automated paper-trading decisions
    for every subscriber auto-trading that symbol, on the next live cycle."""
    try:
        profile_id = int(request.form.get("profile_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id required"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT symbol, engine FROM backtest_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            return jsonify({"error": "Profile not found."}), 404
        if row["engine"] != "sr":
            return jsonify({"error": "Only 'sr' engine profiles can be activated for live trading -- V1/V3/V4 have no live auto-trading path today."}), 400
        now_str = now_ist().isoformat()
        conn.execute("UPDATE backtest_profiles SET is_active_live=0 WHERE symbol=? AND engine='sr'", (row["symbol"],))
        conn.execute(
            "UPDATE backtest_profiles SET is_active_live=1, activated_at=?, activated_by_user_id=? WHERE id=?",
            (now_str, g.user["id"], profile_id),
        )
        conn.commit()
    finally:
        conn.close()
    state.get("sr_active_profile_cache", {}).pop(row["symbol"], None)
    log.warning(f"BACKTEST PROFILE ACTIVATED FOR LIVE TRADING | {row['symbol']}/sr/id={profile_id} by user_id={g.user['id']} -- "
                f"live auto-trading for {row['symbol']} now uses this profile's parameters.")
    return redirect(url_for("backtest_page"))


@app.route("/backtest/profile/deactivate", methods=["POST"])
@auth.roles_required("admin", "developer")
def backtest_profile_deactivate():
    try:
        profile_id = int(request.form.get("profile_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "profile_id required"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT symbol FROM backtest_profiles WHERE id=?", (profile_id,)).fetchone()
        if row:
            conn.execute("UPDATE backtest_profiles SET is_active_live=0 WHERE id=?", (profile_id,))
            conn.commit()
    finally:
        conn.close()
    if row:
        state.get("sr_active_profile_cache", {}).pop(row["symbol"], None)
        log.warning(f"BACKTEST PROFILE DEACTIVATED | {row['symbol']}/sr/id={profile_id} by user_id={g.user['id']} -- live reverts to global defaults.")
    return redirect(url_for("backtest_page"))





def fetch_market_news(max_items=5):
    """
    Fetches recent Indian market news headlines from a free, keyless public
    RSS feed (Economic Times Markets) -- genuine current news, not invented.
    Returns a list of {"title", "pub_date", "link"} dicts, or an empty list
    if the fetch fails (honest degradation, no fake headlines).
    """
    import xml.etree.ElementTree as ET
    try:
        resp = requests.get(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", default="").strip()
            pub_date = item.findtext("pubDate", default="").strip()
            link = item.findtext("link", default="").strip()
            if title:
                items.append({"title": title, "pub_date": pub_date, "link": link})
        return items
    except Exception as e:
        log.warning(f"Market news fetch failed: {e}")
        return []


def generate_premarket_report(symbol):
    """
    Pre-Market Prediction Report (added 2026-07-24): a morning-briefing
    summary for one symbol, aggregating data that's ALREADY computed and
    verified elsewhere in the system -- no new speculative scoring here.

    - Support1/Support2, Resistance1/Resistance2, Major Reversal Zone: from
      the live PDH-formula S/R levels (market_structure.custom_levels).
    - Expected High/Low/Close: an objective ATR-based projection around
      yesterday's close (PDC +/- ATR) -- NOT a directional prediction (no
      claim about which way price will move, just the expected range width).
    - Regime: from the existing ADX-based classifier.
    - System confidence: the GLOBAL score-calibration win-rate (across all
      symbols, from actually-closed trades) -- explicitly labeled as
      system-wide, not symbol-specific, since we don't have enough
      per-symbol closed trades yet to split this reliably.
    """
    structure = state["market_structure_by_symbol"].get(symbol)
    if not structure or not structure.get("custom_levels"):
        return {"symbol": symbol, "error": "No market-structure data yet for this symbol today."}

    levels = structure["custom_levels"]
    atr = structure.get("atr_14")
    regime = structure.get("regime")
    pdc = levels.get("pdc")

    expected_high = round(pdc + atr, 2) if (pdc and atr) else None
    expected_low = round(pdc - atr, 2) if (pdc and atr) else None
    expected_range = round(2 * atr, 2) if atr else None

    try:
        from backtest import score_calibration_report
        calib = score_calibration_report(DB_PATH, min_sample=5)
        system_confidence = calib if calib.get("total_trades") else None
    except Exception:
        system_confidence = None

    return {
        "symbol": symbol,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "support_1": levels.get("support"), "support_2": levels.get("support_reversal"),
        "resistance_1": levels.get("resistance"), "resistance_2": levels.get("resistance_reversal"),
        "major_reversal_zone": f"{levels.get('support_reversal')} - {levels.get('resistance_reversal')}"
                                if levels.get("support_reversal") and levels.get("resistance_reversal") else None,
        "regime": regime,
        "expected_atr": atr,
        "expected_range_points": expected_range,
        "expected_high": expected_high,
        "expected_low": expected_low,
        "expected_close_baseline": pdc,   # unbiased baseline -- NOT a directional prediction
        "system_confidence": system_confidence,
        "note": "Expected High/Low/Range are an ATR-based range projection, not a directional prediction. "
                "System confidence is GLOBAL (all symbols), not specific to this one -- not enough "
                "per-symbol closed trades yet to split reliably.",
    }


def compute_smart_analysis(symbol):
    """
    Comparison analysis for the Smart Dashboard tab:
    - OI-wall-based S/R (legacy, from top-CE/PE-OI strikes) vs formula-based
      S/R (PDH/PDL Range formula) -- side by side, so the user can see where
      they agree/disagree.
    - Reversal/breakout status per level, from the live S/R state machine.
    - Pivot and Max Pain (already-computed reference points).
    - Volume comparison: now vs ~40 minutes ago.
    - OI comparison: now vs ~30 minutes ago.
    All time-lagged comparisons use genuinely logged historical cycles --
    never invented/interpolated data.
    """
    payload = state["last_payload_by_symbol"].get(symbol)
    if not payload:
        return None

    ms = payload.get("market_structure") or {}
    custom_levels = ms.get("custom_levels") or {}
    oi_wall_resistance = payload.get("resistance", [{}])[0].get("strike") if payload.get("resistance") else None
    oi_wall_support = payload.get("support", [{}])[0].get("strike") if payload.get("support") else None
    sr_state = payload.get("sr_state_machine") or {}
    pivots = ms.get("pivots") or {}

    current_rows = payload.get("rows") or []
    current_totals = {
        "ce_oi": sum(r.get("ce_oi") or 0 for r in current_rows),
        "pe_oi": sum(r.get("pe_oi") or 0 for r in current_rows),
        "ce_vol": sum(r.get("ce_vol") or 0 for r in current_rows),
        "pe_vol": sum(r.get("pe_vol") or 0 for r in current_rows),
    }

    # DB-backed time-lagged comparisons are a nice-to-have on top of the
    # already-fetched live payload above -- a DB error here (lock/corruption)
    # must degrade to "comparison unavailable", not take down the whole
    # /smart-analysis page (called once per symbol in a loop).
    oi_30min_ago = None
    vol_40min_ago = None
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        now_ts = now_ist().timestamp()

        def lookback_totals(minutes_ago):
            target_ts = now_ts - minutes_ago * 60
            row = conn.execute(
                "SELECT c.id, c.ts FROM cycles c WHERE c.symbol=? AND c.ts <= ? ORDER BY c.ts DESC LIMIT 1",
                (symbol, dt.datetime.fromtimestamp(target_ts).isoformat()),
            ).fetchone()
            if not row:
                return None
            totals = conn.execute(
                "SELECT SUM(ce_oi) as ce_oi, SUM(pe_oi) as pe_oi, SUM(ce_vol) as ce_vol, SUM(pe_vol) as pe_vol "
                "FROM strikes WHERE cycle_id=?", (row["id"],),
            ).fetchone()
            return {"ts": row["ts"], "ce_oi": totals["ce_oi"] or 0, "pe_oi": totals["pe_oi"] or 0,
                    "ce_vol": totals["ce_vol"] or 0, "pe_vol": totals["pe_vol"] or 0}

        oi_30min_ago = lookback_totals(30)
        vol_40min_ago = lookback_totals(40)
    except Exception as e:
        log.warning(f"compute_smart_analysis: DB lookback failed for {symbol}: {e}")
    finally:
        if conn:
            conn.close()

    def pct_change(now_val, old_val):
        if old_val is None or old_val == 0:
            return None
        return round((now_val - old_val) / old_val * 100, 1)

    oi_comparison = None
    if oi_30min_ago:
        oi_comparison = {
            "ce_oi_now": current_totals["ce_oi"], "ce_oi_30min_ago": oi_30min_ago["ce_oi"],
            "ce_oi_change_pct": pct_change(current_totals["ce_oi"], oi_30min_ago["ce_oi"]),
            "pe_oi_now": current_totals["pe_oi"], "pe_oi_30min_ago": oi_30min_ago["pe_oi"],
            "pe_oi_change_pct": pct_change(current_totals["pe_oi"], oi_30min_ago["pe_oi"]),
        }
    vol_comparison = None
    if vol_40min_ago:
        vol_comparison = {
            "ce_vol_now": current_totals["ce_vol"], "ce_vol_40min_ago": vol_40min_ago["ce_vol"],
            "ce_vol_change_pct": pct_change(current_totals["ce_vol"], vol_40min_ago["ce_vol"]),
            "pe_vol_now": current_totals["pe_vol"], "pe_vol_40min_ago": vol_40min_ago["pe_vol"],
            "pe_vol_change_pct": pct_change(current_totals["pe_vol"], vol_40min_ago["pe_vol"]),
        }

    active_levels = []
    for level_key, st in sr_state.items():
        if isinstance(st, dict) and st.get("state") not in (None, "NO_EDGE"):
            active_levels.append({
                "level": level_key, "state": st.get("state"), "direction": st.get("direction"),
                "distance_label": st.get("distance_label"),
            })

    return {
        "symbol": symbol, "updated": payload.get("updated"),
        "oi_wall_resistance": oi_wall_resistance, "oi_wall_support": oi_wall_support,
        "formula_resistance": custom_levels.get("resistance"), "formula_support": custom_levels.get("support"),
        "formula_resistance_reversal": custom_levels.get("resistance_reversal"),
        "formula_support_reversal": custom_levels.get("support_reversal"),
        "levels_agree": (oi_wall_resistance == custom_levels.get("resistance")) if (oi_wall_resistance and custom_levels.get("resistance")) else None,
        "pivot": pivots.get("P") if pivots else None,
        "max_pain": payload.get("max_pain"),
        "active_sr_states": active_levels,
        "oi_comparison_30min": oi_comparison,
        "volume_comparison_40min": vol_comparison,
    }


def build_multi_symbol_summary():
    """Quick one-line-per-symbol snapshot (bias, LTP, PCR, regime) across ALL
    tracked symbols -- lets the chatbot answer cross-symbol comparison
    questions instead of only knowing about the currently-viewed one."""
    lines = ["=== ALL SYMBOLS SNAPSHOT ==="]
    for sym in SYMBOLS.keys():
        p = state["last_payload_by_symbol"].get(sym)
        if not p:
            continue
        ms = p.get("market_structure") or {}
        lines.append(f"{sym}: LTP={p.get('ltp')} PCR={p.get('pcr')} Bias={p.get('bias')} Regime={ms.get('regime')}")
    return "\n".join(lines) if len(lines) > 1 else "No live data available for any symbol yet."


def build_trade_history_context(symbol, limit=5):
    """Recent closed-trade history for a symbol -- lets the chatbot discuss
    actual trading performance ('how did today's trades go?') using genuine
    logged results, not invented ones."""
    bucket = state["paper_by_symbol"].get(symbol)
    if not bucket or not bucket.get("history"):
        return f"No trade history yet for {symbol} today."
    lines = [f"=== RECENT TRADES: {symbol} (Win rate today: {bucket['wins']}/{bucket['wins']+bucket['losses']+bucket['time_exits']} closed) ==="]
    for t in list(bucket["history"])[:limit]:
        lines.append(f"  {t.get('entry_time')}: {t.get('strike')}{t.get('direction')} @ {t.get('entry_price')} -> "
                      f"{t.get('exit_reason')} ({t.get('points'):+.2f} pts)")
    return "\n".join(lines)


def build_calibration_context():
    """Score-calibration summary (does the Institutional Entry Score
    genuinely predict win-rate?) -- lets the chatbot answer 'has this kind of
    setup worked before?' using real, transparent statistics, not guesses."""
    try:
        from backtest import score_calibration_report
        report = score_calibration_report(DB_PATH, min_sample=5)
        if not report.get("total_trades"):
            return "No score-calibration data yet (not enough closed S/R-engine trades)."
        lines = [f"=== SCORE CALIBRATION (from {report['total_trades']} closed trades) ==="]
        for tier, d in report.get("by_tier", {}).items():
            note = " (sample too small to trust)" if not d["sufficient_sample"] else ""
            lines.append(f"  {tier}: {d['trades']} trades, {d['win_rate']}% win rate{note}")
        return "\n".join(lines)
    except Exception as e:
        return f"Calibration data unavailable: {e}"


def build_chat_context(symbol):
    """
    Builds a readable text summary of everything currently known live for a
    symbol -- LTP/PCR/OI, market structure (ATR/regime/VWAP/Mother-Candle/
    Liquidity-Sweep), Trend Meter, S/R state machine, and near-ATM option
    chain -- so ChatGPT has genuine, current context to answer questions
    about, instead of guessing or using stale training-data knowledge.
    """
    payload = state["last_payload_by_symbol"].get(symbol)
    if not payload:
        return f"No live data available yet for {symbol} (market may be closed, or this symbol hasn't been viewed recently)."

    ms = payload.get("market_structure") or {}
    mother = ms.get("mother_candle") or {}
    sweep = ms.get("liquidity_sweep") or {}
    trend = payload.get("trend_meter") or {}
    sr_state = payload.get("sr_state_machine") or {}
    prev_day = ms.get("prev_day") or {}

    lines = [
        f"=== LIVE DATA SNAPSHOT: {symbol} (updated {payload.get('updated')}) ===",
        f"LTP: {payload.get('ltp')} | ATM Strike: {payload.get('atm')} | PCR: {payload.get('pcr')} | Max Pain: {payload.get('max_pain')}",
        f"Bias: {payload.get('bias')} -- {payload.get('note')}",
    ]

    resistance = payload.get("resistance") or []
    support = payload.get("support") or []
    if resistance:
        lines.append(f"OI-Wall Resistance: {resistance[0]['strike']} (CE OI: {resistance[0]['oi']})")
    if support:
        lines.append(f"OI-Wall Support: {support[0]['strike']} (PE OI: {support[0]['oi']})")

    if ms:
        lines.append(f"Market Regime: {ms.get('regime')} (ADX {ms.get('adx')}) | ATR(14): {ms.get('atr_14')} | VWAP: {ms.get('vwap')}")
    if prev_day:
        lines.append(f"Prev Day: High={prev_day.get('pdh')} Low={prev_day.get('pdl')} Close={prev_day.get('pdc')}")
    if mother.get("found"):
        lines.append(f"Mother Candle: High={mother.get('mother_high')} Low={mother.get('mother_low')}, "
                      f"Inside Bars={mother.get('inside_bar_count')}, "
                      f"Breakout Confirmed={mother.get('breakout_confirmed')} (direction={mother.get('breakout_direction')})")
    if sweep.get("swept"):
        lines.append(f"Liquidity Sweep: {sweep.get('swept')} sweep, reclaimed={sweep.get('reclaimed')}")
    if trend:
        lines.append(f"Trend Meter: {trend.get('score')}/100 ({trend.get('zone')})")

    active_levels = [(k, v) for k, v in (sr_state or {}).items() if isinstance(v, dict) and v.get("state") not in (None, "NO_EDGE")]
    if active_levels:
        lines.append("S/R Engine State:")
        for level_key, st in active_levels:
            extra = f", Institutional Score={st.get('institutional_score')}" if st.get("institutional_score") else ""
            lines.append(f"  - {level_key}: {st.get('state')} (direction={st.get('direction')}){extra}")

    smart = compute_smart_analysis(symbol)
    if smart:
        lines.append(f"OI-Wall vs Formula S/R agree: {smart.get('levels_agree')}")
        if smart.get("oi_comparison_30min"):
            oc = smart["oi_comparison_30min"]
            lines.append(f"OI change (~30min): CE {oc['ce_oi_change_pct']}% | PE {oc['pe_oi_change_pct']}%")
        if smart.get("volume_comparison_40min"):
            vc = smart["volume_comparison_40min"]
            lines.append(f"Volume change (~40min): CE {vc['ce_vol_change_pct']}% | PE {vc['pe_vol_change_pct']}%")

    rows = payload.get("rows") or []
    near_atm = sorted(rows, key=lambda r: abs(r["strike"] - (payload.get("atm") or 0)))[:5]
    if near_atm:
        lines.append("Near-ATM Option Chain (CE Signal | Strike | PE Signal):")
        for r in near_atm:
            lines.append(f"  {r['strike']}: CE OI={r['ce_oi']} ({r['ce_signal']}) | PE OI={r['pe_oi']} ({r['pe_signal']})")

    paper = payload.get("paper") or {}
    if paper.get("open_trade"):
        t = paper["open_trade"]
        lines.append(f"Open Paper Trade: {t.get('strike')}{t.get('direction')} @ {t.get('entry_price')}, target {t.get('target_price')}, SL {t.get('sl_price')}")

    return "\n".join(lines)


@app.route("/api/chat", methods=["POST"])
@auth.roles_required("admin", "developer")
def chat_api():
    """
    Smart chatbot endpoint -- uses the server-configured Groq API key
    (GROQ_API_KEY in .env; Groq exposes an OpenAI-compatible chat-completions
    API, so only the base URL/model/key changed from the original OpenAI
    integration, not the request/response shape), the user's question, and
    recent conversation history (for follow-up questions), builds a rich
    live data-context (current symbol detail + all-symbols snapshot + trade
    history + score-calibration stats + news), and asks the model to analyze.
    """
    import requests as req_lib
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    symbol = (data.get("symbol") or DEFAULT_SYMBOL).strip()
    history = data.get("history") or []   # [{"role": "user"/"assistant", "content": "..."}]

    if not GROQ_API_KEY:
        return jsonify({"error": "Chat is not configured on this server (missing GROQ_API_KEY)."}), 503
    if not question:
        return jsonify({"error": "No question provided."}), 400
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    if not isinstance(history, list):
        history = []
    history = history[-10:]   # cap conversation memory -- keeps token usage bounded

    context = build_chat_context(symbol)
    context += "\n\n" + build_multi_symbol_summary()
    context += "\n\n" + build_trade_history_context(symbol)
    context += "\n\n" + build_calibration_context()

    news_items = fetch_market_news(max_items=5)
    if news_items:
        news_text = "\n\n=== RECENT MARKET NEWS (Economic Times, genuinely fetched just now) ===\n" + "\n".join(
            f"- {n['title']} ({n['pub_date']})" for n in news_items
        )
        context += news_text
    else:
        context += "\n\n=== RECENT MARKET NEWS ===\nNews fetch failed or unavailable right now -- don't invent any headlines."

    system_prompt = (
        "You are a data-analyst assistant embedded in a live NSE/MCX options dashboard -- NOT a "
        "licensed financial advisor. You will be given a live data snapshot for one symbol (OI, "
        "LTP, PCR, market structure, Mother Candle, Liquidity Sweep, S/R state machine), a "
        "snapshot of ALL tracked symbols, recent trade history, score-calibration statistics, and "
        "genuine news headlines (when available). Answer using ONLY the data provided -- do not "
        "invent numbers, prices, trade outcomes, or news. If asked about something not in the "
        "context, say so honestly rather than guessing. You may explain patterns, structure, and "
        "evidence clearly, and note what would need to happen for a bullish/bearish case -- but do "
        "NOT give direct 'buy now' / 'sell now' instructions or position-sizing advice; frame "
        "things as analysis the user can weigh themselves, not as a recommendation. Be concise "
        "(mobile screen, a few short paragraphs). You have access to the recent conversation -- "
        "use it for follow-up questions without re-explaining everything from scratch."
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": h.get("role"), "content": h.get("content")} for h in history
                     if h.get("role") in ("user", "assistant") and h.get("content"))
    messages.append({"role": "user", "content": f"{context}\n\n=== USER QUESTION ===\n{question}"})

    try:
        resp = req_lib.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_CHAT_MODEL,
                "messages": messages,
                "max_tokens": 500,
                "temperature": 0.3,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            err_detail = resp.json().get("error", {}).get("message", resp.text[:200]) if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:200]
            return jsonify({"error": f"Groq API error ({resp.status_code}): {err_detail}"}), 502
        reply = resp.json()["choices"][0]["message"]["content"]
        return jsonify({"reply": reply})
    except req_lib.exceptions.Timeout:
        return jsonify({"error": "Groq API request timed out."}), 504
    except Exception as e:
        log.warning(f"Chat API error: {e}")
        return jsonify({"error": f"Request failed: {e}"}), 500


def _validate_target_sl(direction, entry_price, target_price, sl_price):
    """These are BUY-only CE/PE positions -- target must be above entry, SL
    below. Was unenforced pre-Phase-2 because the worst case was a no-op
    Exit-button click; now that SL is wired into an AUTOMATIC, wallet-
    crediting exit (see update_paper_orders), an inverted value could
    mislabel a loss as "TARGET HIT" with a real wallet credit attached, so
    both are validated here before the trade is ever created/edited.
    Returns an error string, or None if valid."""
    if target_price is not None and target_price <= entry_price:
        return "target_price must be above the entry/limit price for a CE/PE buy."
    if sl_price is not None and sl_price >= entry_price:
        return "sl_price must be below the entry/limit price for a CE/PE buy."
    return None


def _validate_order_type_requirements(order_type, target_price, sl_price, stop_price):
    """Cross-field requirements per order type, checked BEFORE any DB write or
    wallet action -- mirrors what a real broker's order-entry form enforces,
    so an order that's SUPPOSED to be risk-managed (STOP needs a trigger,
    BRACKET/COVER need a mandatory SL) can't silently end up without one.
    Returns an error string, or None if valid."""
    if order_type == "STOP" and stop_price is None:
        return "stop_price is required for a STOP order."
    if order_type == "BRACKET" and (target_price is None or sl_price is None):
        return "BRACKET orders require both target_price and sl_price."
    if order_type == "COVER" and sl_price is None:
        return "COVER orders require sl_price."
    return None


@app.route("/manual-trading")
@auth.subscription_required
def manual_trading_page():
    """
    Manual paper-trading page: trader enters CE/PE trades by hand (market OR
    limit entry), sets target/SL (auto-exits on either, see
    update_paper_orders), and can also exit manually with one click.
    COMPLETELY SEPARATE from the auto S/R Engine's paper_trades -- lets a
    human's manual judgment be honestly compared against the automated
    system, side by side, using the SAME live market data.
    Paper-trading only -- no real orders are ever placed. Strictly per-user:
    every query here is scoped to the logged-in user's own trades, admin
    included -- there is no cross-user visibility on this page for anyone.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    uid = g.user["id"]
    open_trades = conn.execute(
        "SELECT * FROM paper_orders WHERE user_id=? AND status='OPEN' ORDER BY entry_ts DESC", (uid,)
    ).fetchall()
    pending_trades = conn.execute(
        "SELECT * FROM paper_orders WHERE user_id=? AND status='PENDING' ORDER BY id DESC", (uid,)
    ).fetchall()
    closed_trades = conn.execute(
        "SELECT * FROM paper_orders WHERE user_id=? AND status='CLOSED' ORDER BY entry_ts DESC LIMIT 50", (uid,)
    ).fetchall()
    conn.close()

    def _num(v, default=0):
        # Defensive coercion -- guards display against any pre-existing rows
        # written before qty validation was added at the /enter endpoint.
        try:
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    open_list = [dict(r) for r in open_trades]
    pending_list = [dict(r) for r in pending_trades]
    closed_list = [dict(r) for r in closed_trades]
    total_points = sum(_num(t.get("points")) for t in closed_list)
    total_amount = sum(_num(t.get("points")) * _num(t.get("qty"), 1) for t in closed_list)
    wins = sum(1 for t in closed_list if _num(t.get("points")) > 0)
    losses = sum(1 for t in closed_list if _num(t.get("points")) < 0)

    return render_template("manual_trading.html", symbols=list(SYMBOLS.keys()),
                            open_trades=open_list, pending_trades=pending_list, closed_trades=closed_list,
                            total_points=round(total_points, 2), total_amount=round(total_amount, 2),
                            wins=wins, losses=losses, wallet_balance=g.user["wallet_balance"],
                            delete_enabled=state.get("manual_trade_delete_enabled"))


@app.route("/api/manual-trade/option-chain/<symbol>")
@auth.subscription_required
def api_manual_trade_option_chain(symbol):
    """Returns the CURRENT live option-chain rows for a symbol (strike, CE/PE
    LTP) -- reuses the ALREADY-live payload, no new data-fetch. Powers the
    strike/premium dropdown on the manual-trading entry form."""
    payload = state["last_payload_by_symbol"].get(symbol)
    if not payload:
        return jsonify({"error": "No live data for this symbol yet."}), 404
    rows = payload.get("rows") or []
    return jsonify({
        "symbol": symbol, "atm": payload.get("atm"), "ltp": payload.get("ltp"),
        "lot_size": LOT_SIZES.get(symbol, 1),
        "rows": [{"strike": r.get("strike"), "ce_ltp": r.get("ce_ltp"), "pe_ltp": r.get("pe_ltp")} for r in rows],
    })


@app.route("/api/manual-trade/enter", methods=["POST"])
@auth.subscription_required
def api_manual_trade_enter():
    """Enters a paper order -- MARKET/BRACKET/COVER fill immediately at the
    current live premium; LIMIT/STOP are queued as PENDING, filled by
    update_paper_orders once price crosses limit_price (at-or-better) or
    stop_price (breakout trigger). Always trade_source='MANUAL' -- AUTO
    orders are inserted exclusively by fanout_auto_trade_entry, never through
    this user-facing route. Wallet-linked: an immediate-fill order debits the
    wallet now (rejects if insufficient); a PENDING order only does a
    non-debiting affordability ESTIMATE at placement -- the real debit
    happens atomically at fill time against the ACTUAL fill price and
    CURRENT balance (see billing.debit_if_sufficient), never at placement."""
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol")
    strike = data.get("strike")
    direction = data.get("direction")   # "CE" or "PE"
    order_type = (data.get("order_type") or "MARKET").upper()

    if not symbol or not strike or direction not in ("CE", "PE"):
        return jsonify({"error": "symbol, strike, and direction (CE/PE) are required."}), 400
    if order_type not in ("MARKET", "LIMIT", "STOP", "BRACKET", "COVER"):
        return jsonify({"error": "order_type must be one of MARKET, LIMIT, STOP, BRACKET, COVER."}), 400

    raw_qty = data.get("qty")
    try:
        qty = int(raw_qty) if raw_qty not in (None, "") else 1
        if qty <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be a positive integer."}), 400

    def _opt_float(key):
        v = data.get(key)
        if v in (None, ""):
            return None
        return float(v)

    try:
        target_price = _opt_float("target_price")
        sl_price = _opt_float("sl_price")
        stop_price = _opt_float("stop_price")
    except (TypeError, ValueError):
        return jsonify({"error": "target_price/sl_price/stop_price must be numbers."}), 400

    err = _validate_order_type_requirements(order_type, target_price, sl_price, stop_price)
    if err:
        return jsonify({"error": err}), 400

    trailing_stop_enabled = bool(data.get("trailing_stop_enabled"))
    if trailing_stop_enabled and target_price is None:
        return jsonify({"error": "trailing_stop_enabled requires target_price to be set "
                                  "(trailing distance is measured against it)."}), 400
    try:
        trailing_trigger_pct = _opt_float("trailing_trigger_pct") if trailing_stop_enabled else None
        trailing_giveback_pct = _opt_float("trailing_giveback_pct") if trailing_stop_enabled else None
        breakeven_trigger_pct = _opt_float("breakeven_trigger_pct") if trailing_stop_enabled else None
    except (TypeError, ValueError):
        return jsonify({"error": "trailing_trigger_pct/trailing_giveback_pct/breakeven_trigger_pct must be numbers."}), 400

    intraday_only = 1 if order_type in ("BRACKET", "COVER") else 0
    now = now_ist()
    uid = g.user["id"]

    if order_type in ("LIMIT", "STOP"):
        if order_type == "LIMIT":
            limit_price = data.get("limit_price")
            try:
                limit_price = float(limit_price)
                if limit_price <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"error": "limit_price must be a positive number for a LIMIT order."}), 400
            est_price = limit_price
        else:   # STOP
            if stop_price <= 0:
                return jsonify({"error": "stop_price must be a positive number."}), 400
            limit_price = None
            # Affordability ESTIMATE only -- once triggered, a STOP fills at
            # whatever the current price is, which may exceed stop_price;
            # the real check happens atomically at fill time.
            est_price = stop_price

        err = _validate_target_sl(direction, est_price, target_price, sl_price)
        if err:
            return jsonify({"error": err}), 400
        if g.user["wallet_balance"] < est_price * qty:
            return jsonify({"error": f"Insufficient wallet balance for this order even at your estimated entry "
                                      f"price. Call {SUPPORT_PHONE_NUMBER} to recharge."}), 400
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO paper_orders (user_id, symbol, strike, direction, trade_source, order_type,
                                          limit_price, stop_price, target_price, sl_price, qty, status, wallet_linked,
                                          trailing_stop_enabled, trailing_trigger_pct, trailing_giveback_pct,
                                          breakeven_trigger_pct, intraday_only)
               VALUES (?,?,?,?, 'MANUAL', ?, ?,?,?,?,?, 'PENDING', 1, ?,?,?,?,?)""",
            (uid, symbol, strike, direction, order_type, limit_price, stop_price, target_price, sl_price, qty,
             1 if trailing_stop_enabled else 0, trailing_trigger_pct, trailing_giveback_pct,
             breakeven_trigger_pct, intraday_only),
        )
        conn.commit()
        conn.close()
        log.info(f"MANUAL {order_type} ORDER PLACED | user_id={uid} {symbol} {strike}{direction} @ "
                 f"{'limit '+str(limit_price) if order_type=='LIMIT' else 'stop '+str(stop_price)} x{qty}")
        return jsonify({"status": "ok", "order_type": order_type, "limit_price": limit_price, "stop_price": stop_price})

    # MARKET / BRACKET / COVER -- immediate fill at the current live premium
    payload = state["last_payload_by_symbol"].get(symbol)
    if not payload:
        return jsonify({"error": f"No live data for {symbol} yet -- cannot determine current premium."}), 400
    row = next((r for r in (payload.get("rows") or []) if r.get("strike") == strike), None)
    if not row:
        return jsonify({"error": f"Strike {strike} not found in current {symbol} chain."}), 400
    entry_price = row.get("ce_ltp") if direction == "CE" else row.get("pe_ltp")
    if not entry_price:
        return jsonify({"error": "Current premium for this strike is genuinely unavailable (0 or missing)."}), 400

    err = _validate_target_sl(direction, entry_price, target_price, sl_price)
    if err:
        return jsonify({"error": err}), 400

    new_balance = billing.debit_if_sufficient(uid, round(entry_price * qty, 2), "trade_entry",
                                               note=f"{symbol} {strike}{direction} entry")
    if new_balance is None:
        return jsonify({"error": f"Insufficient wallet balance. Call {SUPPORT_PHONE_NUMBER} to recharge."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO paper_orders (user_id, symbol, strike, direction, trade_source, order_type, entry_price,
                                      target_price, sl_price, qty, entry_time, entry_ts, status, wallet_linked,
                                      trailing_stop_enabled, trailing_trigger_pct, trailing_giveback_pct,
                                      breakeven_trigger_pct, intraday_only)
           VALUES (?,?,?,?, 'MANUAL', ?, ?,?,?,?,?,?, 'OPEN', 1, ?,?,?,?,?)""",
        (uid, symbol, strike, direction, order_type, entry_price, target_price, sl_price, qty,
         now.strftime("%H:%M:%S"), now.timestamp(),
         1 if trailing_stop_enabled else 0, trailing_trigger_pct, trailing_giveback_pct,
         breakeven_trigger_pct, intraday_only),
    )
    conn.commit()
    conn.close()
    log.info(f"MANUAL PAPER ORDER ENTERED | user_id={uid} {symbol} {strike}{direction} @ {entry_price} x{qty} | "
             f"Type={order_type} Target={target_price} SL={sl_price} | wallet -> {new_balance:.2f}")
    return jsonify({"status": "ok", "entry_price": entry_price, "wallet_balance": new_balance})


@app.route("/api/manual-trade/exit", methods=["POST"])
@auth.subscription_required
def api_manual_trade_exit():
    """Exits an OPEN manual paper-trade at the CURRENT live premium (one-click
    market exit). Paper-trading only. Uses an atomic status-guarded UPDATE
    (WHERE status='OPEN', rowcount checked) so a concurrent auto-exit from
    update_paper_orders can never race this into a double wallet
    credit for the same trade -- whichever write lands first wins, the other
    sees rowcount=0 and does nothing further."""
    data = request.get_json(force=True) or {}
    trade_id = data.get("id")
    if not trade_id:
        return jsonify({"error": "Trade id is required."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM paper_orders WHERE id=?", (trade_id,)).fetchone()
    if not trade or trade["user_id"] != g.user["id"]:
        conn.close()
        return jsonify({"error": "Trade not found."}), 404
    if trade["status"] != "OPEN":
        conn.close()
        return jsonify({"error": "Trade not found or already closed."}), 404

    payload = state["last_payload_by_symbol"].get(trade["symbol"])
    if not payload:
        conn.close()
        return jsonify({"error": f"No live data for {trade['symbol']} right now -- cannot determine exit premium."}), 400
    row = next((r for r in (payload.get("rows") or []) if r.get("strike") == trade["strike"]), None)
    exit_price = (row.get("ce_ltp") if trade["direction"] == "CE" else row.get("pe_ltp")) if row else None
    if not exit_price:
        conn.close()
        return jsonify({"error": "Current premium genuinely unavailable for exit."}), 400

    points = round(exit_price - trade["entry_price"], 2)
    try:
        qty = int(trade["qty"]) if "qty" in trade.keys() and trade["qty"] else 1
    except (TypeError, ValueError):
        qty = 1
    amount = round(points * qty, 2)
    now_str = now_ist().strftime("%H:%M:%S")
    cur = conn.execute(
        "UPDATE paper_orders SET exit_price=?, exit_time=?, exit_reason='MANUAL EXIT', points=?, status='CLOSED' "
        "WHERE id=? AND status='OPEN'",
        (exit_price, now_str, points, trade_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "Trade already closed."}), 409
    conn.close()

    new_balance = None
    if trade["wallet_linked"]:
        # Credit the FULL exit proceeds (exit_price*qty), NOT `amount`
        # (points*qty) -- entry already debited the full entry cost
        # (entry_price*qty), so crediting only the net points here would
        # double-count that debit instead of returning principal+P&L. `amount`
        # (points*qty) is still the right value to show the user below -- it
        # IS the P&L -- just not what gets credited to the wallet ledger.
        proceeds = round(exit_price * qty, 2)
        new_balance = billing.create_wallet_transaction(trade["user_id"], proceeds, "trade_pnl",
                                                          note=f"{trade['symbol']} {trade['strike']}{trade['direction']} MANUAL EXIT")
    log.info(f"MANUAL PAPER TRADE EXITED | user_id={trade['user_id']} {trade['symbol']} {trade['strike']}{trade['direction']} "
             f"@ {exit_price} | Points={points} Qty={qty} Amount={amount} | wallet -> {new_balance}")
    return jsonify({"status": "ok", "exit_price": exit_price, "points": points, "qty": qty, "amount": amount,
                     "wallet_balance": new_balance})


@app.route("/api/manual-trade/edit", methods=["POST"])
@auth.subscription_required
def api_manual_trade_edit():
    """Edits an OPEN manual trade's target/SL -- broker-style order modification."""
    data = request.get_json(force=True) or {}
    trade_id = data.get("id")
    target_price = data.get("target_price")
    sl_price = data.get("sl_price")
    if not trade_id:
        return jsonify({"error": "Trade id is required."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM paper_orders WHERE id=?", (trade_id,)).fetchone()
    if not trade or trade["user_id"] != g.user["id"] or trade["status"] != "OPEN":
        conn.close()
        return jsonify({"error": "Trade not found or already closed."}), 404
    err = _validate_target_sl(trade["direction"], trade["entry_price"], target_price, sl_price)
    if err:
        conn.close()
        return jsonify({"error": err}), 400
    conn.execute("UPDATE paper_orders SET target_price=?, sl_price=? WHERE id=?", (target_price, sl_price, trade_id))
    conn.commit()
    conn.close()
    log.info(f"MANUAL PAPER TRADE EDITED | id={trade_id} | New Target={target_price} New SL={sl_price}")
    return jsonify({"status": "ok"})


@app.route("/api/manual-trade/cancel", methods=["POST"])
@auth.subscription_required
def api_manual_trade_cancel():
    """Cancels a PENDING limit order. No wallet action -- placement never
    debited anything (see api_manual_trade_enter's LIMIT path), so there is
    nothing to refund. Atomic status-guarded UPDATE, same race-safety
    reasoning as the exit route."""
    data = request.get_json(force=True) or {}
    trade_id = data.get("id")
    if not trade_id:
        return jsonify({"error": "Trade id is required."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM paper_orders WHERE id=?", (trade_id,)).fetchone()
    if not trade or trade["user_id"] != g.user["id"]:
        conn.close()
        return jsonify({"error": "Order not found."}), 404
    cur = conn.execute("UPDATE paper_orders SET status='CANCELLED' WHERE id=? AND status='PENDING'", (trade_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Order not found or already filled/cancelled."}), 409
    log.info(f"MANUAL LIMIT ORDER CANCELLED | user_id={g.user['id']} id={trade_id}")
    return jsonify({"status": "ok"})


@app.route("/api/manual-trade/my-trades")
@auth.subscription_required
def api_manual_trade_my_trades():
    """JSON snapshot of the logged-in user's own open/pending/closed manual
    trades + current wallet balance -- polled client-side (see
    manual_trading.html) so a server-side auto-exit or limit-fill shows up
    without the user needing to reload the page."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    uid = g.user["id"]
    open_trades = conn.execute("SELECT * FROM paper_orders WHERE user_id=? AND status='OPEN' ORDER BY entry_ts DESC", (uid,)).fetchall()
    pending_trades = conn.execute("SELECT * FROM paper_orders WHERE user_id=? AND status='PENDING' ORDER BY id DESC", (uid,)).fetchall()
    closed_trades = conn.execute("SELECT * FROM paper_orders WHERE user_id=? AND status='CLOSED' ORDER BY entry_ts DESC LIMIT 50", (uid,)).fetchall()
    conn.close()
    wallet_row = sqlite3.connect(DB_PATH)
    balance = wallet_row.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()
    wallet_row.close()
    return jsonify({
        "open": [dict(r) for r in open_trades],
        "pending": [dict(r) for r in pending_trades],
        "closed": [dict(r) for r in closed_trades],
        "wallet_balance": balance[0] if balance else None,
    })


@app.route("/api/risk/portfolio")
@auth.subscription_required
def api_risk_portfolio():
    """Milestone 6 (AI Risk Manager): the logged-in user's live risk
    snapshot -- exposure, portfolio heat, margin utilization, daily P&L,
    drawdown, concentration, correlation, Greeks exposure, and any
    active alerts. Read-only: computing this also persists it via
    agents.risk_manager.risk_store (see agents/risk_manager/api.py's
    get_portfolio_snapshot), same "every risk decision logged" posture
    as the Promotion Risk Gate. Polled client-side by manual_trading.html's
    risk widget."""
    return jsonify(risk_api.get_portfolio_snapshot(user_id=g.user["id"]))


@app.route("/api/risk/alerts")
@auth.subscription_required
def api_risk_alerts():
    """Recent risk alerts for the logged-in user (agents.risk_manager.risk_store,
    already-persisted history -- does not compute a fresh snapshot; see
    /api/risk/portfolio for that)."""
    limit = min(int(request.args.get("limit", 20)), 100)
    return jsonify({"alerts": risk_api.get_recent_alerts(user_id=g.user["id"], limit=limit)})


@app.route("/api/manual-trade/delete", methods=["POST"])
@auth.roles_required("admin", "developer")
def api_manual_trade_delete():
    """Deletes manual paper-trade(s) -- single, a list of selected ids, or
    all. GATED behind state['manual_trade_delete_enabled'] (off by default,
    opt-in via /dev-settings) -- keeps trade records honest and permanent
    unless deliberately enabled. NOTE (Phase 2): mode='all' now wipes EVERY
    user's trades, not just admin/developer test data, now that this feature
    is open to subscribers -- code unchanged, but the blast radius is bigger
    than it used to be."""
    if not state.get("manual_trade_delete_enabled"):
        return jsonify({"error": "Delete is disabled. Enable it in /dev-settings first."}), 403

    data = request.get_json(force=True) or {}
    mode = data.get("mode")   # "single" | "selected" | "all"
    conn = sqlite3.connect(DB_PATH)
    if mode == "single":
        trade_id = data.get("id")
        if not trade_id:
            conn.close()
            return jsonify({"error": "Trade id is required."}), 400
        conn.execute("DELETE FROM paper_orders WHERE id=?", (trade_id,))
        deleted = conn.total_changes
    elif mode == "selected":
        ids = data.get("ids") or []
        if not ids:
            conn.close()
            return jsonify({"error": "No trade ids selected."}), 400
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM paper_orders WHERE id IN ({placeholders})", ids)
        deleted = conn.total_changes
    elif mode == "all":
        conn.execute("DELETE FROM paper_orders")
        deleted = conn.total_changes
    else:
        conn.close()
        return jsonify({"error": "mode must be 'single', 'selected', or 'all'."}), 400
    conn.commit()
    conn.close()
    log.info(f"MANUAL PAPER TRADE(S) DELETED | mode={mode} | data={data}")
    return jsonify({"status": "ok", "deleted": deleted})


@app.route("/auto-trading-settings", methods=["GET", "POST"])
@auth.subscription_required
def auto_trading_settings_page():
    """Per-subscriber self-service page: opt in/out of AI Auto-Trading for
    the Swing S/R Engine and/or the Scalp Engine, independently, each with
    its own qty. Distinct from /dev-settings' GLOBAL admin toggles (which
    turn those engines' OWN system-wide reference-trade tracking on/off
    entirely) -- this is each subscriber's individual choice to ALSO have
    their own wallet participate whenever those (unchanged) engines fire a
    genuine trigger. Feeds fanout_auto_trade_entry, which reads this same
    table. `qty` here is LOTS (same convention as Manual Trading's qty
    input) -- fanout_auto_trade_entry multiplies by each triggered symbol's
    own LOT_SIZES entry to get the actual order quantity."""
    uid = g.user["id"]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if request.method == "POST":
        now_str = now_ist().isoformat()
        for engine in ("SWING", "SCALP"):
            enabled = 1 if f"{engine.lower()}_enabled" in request.form else 0
            try:
                qty = max(1, int(request.form.get(f"{engine.lower()}_qty") or 1))
            except (TypeError, ValueError):
                qty = 1
            conn.execute(
                """INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(user_id, engine) DO UPDATE SET
                       enabled=excluded.enabled, qty=excluded.qty, updated_at=excluded.updated_at""",
                (uid, engine, enabled, qty, now_str, now_str),
            )
        conn.commit()
        log.info(f"AUTO-TRADING SETTINGS UPDATED | user_id={uid}")
    rows = conn.execute("SELECT engine, enabled, qty FROM user_auto_trading_settings WHERE user_id=?", (uid,)).fetchall()
    conn.close()
    settings = {r["engine"]: {"enabled": bool(r["enabled"]), "qty": r["qty"]} for r in rows}
    for engine in ("SWING", "SCALP"):
        settings.setdefault(engine, {"enabled": False, "qty": 1})
    return render_template("auto_trading_settings.html", settings=settings)


@app.route("/api/auto-trading-settings/toggle", methods=["POST"])
@auth.subscription_required
def api_auto_trading_settings_toggle():
    """JSON single-engine toggle -- same underlying table as the full-page
    form above, for a faster inline on/off without a full page submit."""
    data = request.get_json(force=True) or {}
    engine = (data.get("engine") or "").upper()
    if engine not in ("SWING", "SCALP"):
        return jsonify({"error": "engine must be 'SWING' or 'SCALP'."}), 400
    enabled = 1 if data.get("enabled") else 0
    try:
        qty = max(1, int(data.get("qty") or 1))
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be a positive integer."}), 400
    uid = g.user["id"]
    now_str = now_ist().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, engine) DO UPDATE SET
               enabled=excluded.enabled, qty=excluded.qty, updated_at=excluded.updated_at""",
        (uid, engine, enabled, qty, now_str, now_str),
    )
    conn.commit()
    conn.close()
    log.info(f"AUTO-TRADING TOGGLE | user_id={uid} engine={engine} enabled={bool(enabled)} qty={qty}")
    return jsonify({"status": "ok", "engine": engine, "enabled": bool(enabled), "qty": qty})


@app.route("/signal-history")
@auth.roles_required("admin", "developer")
def signal_history_page():
    """
    Clean, dated, chronological listing of ALL logged paper-trades (signals)
    -- read-only, uses ONLY already-existing paper_trades data (no new risk,
    no order-placement). Lets the trader look back at any past-dated signal
    instead of only seeing "the latest" -- signals never appear to vanish.
    Supports optional date-range and symbol filtering via query params.
    """
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    symbol_filter = request.args.get("symbol")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM paper_trades WHERE 1=1"
    params = []
    if symbol_filter:
        query += " AND symbol=?"
        params.append(symbol_filter)
    if date_from:
        query += " AND date(entry_ts, 'unixepoch') >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date(entry_ts, 'unixepoch') <= ?"
        params.append(date_to)
    query += " ORDER BY entry_ts DESC LIMIT 500"

    rows = conn.execute(query, params).fetchall()
    trades = []
    for r in rows:
        d = dict(r)
        d["entry_datetime_ist"] = None
        if d.get("entry_ts"):
            try:
                d["entry_datetime_ist"] = (dt.datetime.utcfromtimestamp(d["entry_ts"]) + dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                pass
        trades.append(d)
    conn.close()

    total = len(trades)
    wins = sum(1 for t in trades if (t.get("points") or 0) > 0)
    losses = sum(1 for t in trades if (t.get("points") or 0) < 0)
    total_points = sum(t.get("points") or 0 for t in trades)

    return render_template("signal_history.html", trades=trades, symbols=list(SYMBOLS.keys()),
                            total=total, wins=wins, losses=losses, total_points=round(total_points, 2),
                            date_from=date_from or "", date_to=date_to or "", symbol_filter=symbol_filter or "")


@app.route("/calibration")
@auth.roles_required("admin", "developer")
def calibration_page():
    """
    Score Calibration Tracker -- honest, transparent statistics (NOT machine
    learning) checking whether the Institutional Entry Score genuinely
    predicts win-rate, broken down by score-tier and by regime-at-entry.
    Doesn't change any behavior automatically; a human uses this to decide
    whether/how to adjust thresholds once there's enough real data.
    """
    from backtest import score_calibration_report
    report = score_calibration_report(DB_PATH, min_sample=5)
    return render_template("calibration.html", report=report)


@app.route("/signals")
@auth.subscription_required
def profitable_signals_page():
    """
    Cross-symbol view: scans every tracked symbol's S/R Engine state (already
    being computed continuously by the background threads for all symbols,
    not just the currently-viewed one) and surfaces anything ARMED/CONFIRMED/
    ACTIVE in one place -- instead of switching through 14 symbol dropdowns
    one at a time to find what's actionable right now.
    """
    interesting_states = {"ARMED", "CONFIRMED", "ACTIVE", "TARGET_HIT", "STOPPED"}
    rows = []
    for symbol in SYMBOLS.keys():
        bucket = state["sr_state_by_symbol"].get(symbol, {})
        for level_key, st in bucket.items():
            if st.get("state") in interesting_states:
                rows.append({
                    "symbol": symbol, "level": level_key, "state": st.get("state"),
                    "direction": st.get("direction"), "level_price": st.get("level_price"),
                    "distance_pts": st.get("distance_pts"), "triggered": st.get("triggered"),
                    "entry_price": st.get("entry_price"), "target1": st.get("target1"), "sl": st.get("sl"),
                    "risk_reward": st.get("risk_reward"), "institutional_score": st.get("institutional_score"),
                    "institutional_tier": st.get("institutional_tier"),
                    "since": st.get("since").strftime("%H:%M:%S") if st.get("since") else None,
                })

    state_rank = {"ACTIVE": 0, "CONFIRMED": 1, "ARMED": 2, "TARGET_HIT": 3, "STOPPED": 4}
    rows.sort(key=lambda r: state_rank.get(r["state"], 9))

    return render_template("signals.html", rows=rows, symbol_count=len(SYMBOLS))


_position_monitor_angel = None


def _get_position_monitor_angel():
    """Reuses the SAME AngelOneFetcher instance the live trading loop uses
    (_shared_angel_fetcher, set once start_all_symbol_loops() runs) --
    brokers including Angel One typically allow only one active session per
    client id, so opening a second independent login here would risk
    invalidating the main loop's session. Only falls back to creating a
    fresh instance if the main loop hasn't started yet (e.g. this route hit
    during startup, or the module used standalone)."""
    global _position_monitor_angel
    if _shared_angel_fetcher is not None:
        return _shared_angel_fetcher
    if _position_monitor_angel is None:
        log.warning("Position monitor opening its own Angel One session -- "
                    "the main trading loop's shared instance isn't up yet.")
        _position_monitor_angel = AngelOneFetcher()
    return _position_monitor_angel


def _fetch_today_price_action(symbol):
    """
    Fetches today's price-action summary (open/high/low/current + simple
    reversal-points) from the ALREADY-logged cycles table -- lets the
    advisor genuinely answer 'what happened today' narrative questions
    (e.g. 'hero to zero'), not just report the current live snapshot.
    Read-only, uses only already-collected data. Returns None if nothing
    genuinely logged for today.
    """
    try:
        today_ist = getISTDateString_py()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cycles = conn.execute(
            "SELECT time, underlying_ltp FROM cycles WHERE symbol=? AND date=? ORDER BY ts ASC",
            (symbol, today_ist),
        ).fetchall()
        conn.close()
        if len(cycles) < 5:
            return None
        prices = [c["underlying_ltp"] for c in cycles if c["underlying_ltp"] is not None]
        if not prices:
            return None
        day_open, day_high, day_low, current = prices[0], max(prices), min(prices), prices[-1]

        reversal_idx = find_reversal_points(prices, window=5)
        reversal_summary = [
            {"time": cycles[p["index"]]["time"], "type": p["type"], "value": p["value"]}
            for p in reversal_idx[-4:]   # last few reversal-points -- enough for a "what happened" narrative without overwhelming
        ]
        return {
            "day_open": day_open, "day_high": day_high, "day_low": day_low, "current": current,
            "reversal_points": reversal_summary,
        }
    except Exception as e:
        log.warning(f"Advisor: today's price-action fetch failed for {symbol}: {e}")
        return None


def getISTDateString_py():
    """Python-side equivalent of the frontend's getISTDateString -- explicit
    IST(+5:30) date, not server-local or UTC, matching the same fix used
    for the chart pages' date-boundary handling."""
    now_utc = dt.datetime.now(IST)
    return now_utc.date().isoformat()


def _fetch_last_known_from_db(symbol):
    """Fallback for the advisor when a symbol has no live in-memory payload
    yet this session (e.g. right after a restart, or market closed) --
    reads the MOST RECENT logged cycle/strikes/snapshot from the database
    instead. Returns None if genuinely nothing has ever been logged."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cycle = conn.execute(
            "SELECT * FROM cycles WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
        if not cycle:
            conn.close()
            return None
        strikes = conn.execute(
            "SELECT * FROM strikes WHERE cycle_id=?", (cycle["id"],)
        ).fetchall()
        snap = conn.execute(
            "SELECT custom_levels_json FROM market_structure_snapshots WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
        conn.close()

        v1_levels = {}
        if snap and snap["custom_levels_json"]:
            try:
                v1_levels = json.loads(snap["custom_levels_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "ltp": cycle["underlying_ltp"], "pcr": cycle["pcr"], "atm": cycle["atm"], "as_of": cycle["ts"],
            "rows": [dict(r) for r in strikes], "v1_levels": v1_levels,
        }
    except Exception as e:
        log.warning(f"Advisor DB-fallback failed for {symbol}: {e}")
        return None


def _gather_advisor_context():
    """Assembles context for the advisory chatbot from data we ALREADY
    compute/track live -- never fetches anything new, never invents data."""
    context = {"open_positions": [], "symbols_summary": [], "calibration": None}

    try:
        angel_monitor = _get_position_monitor_angel()
        raw_positions = angel_monitor.get_open_positions()
        for pos in raw_positions:
            matched_symbol = match_symbol_prefix(pos["symbol"])
            if not matched_symbol:
                continue
            payload = state["last_payload_by_symbol"].get(matched_symbol) or {}
            payload_rows = payload.get("rows") or []
            rows_objs = [StrikeRow(**{k: v for k, v in r.items() if k in StrikeRow.__dataclass_fields__}) for r in payload_rows] if payload_rows else []
            atm = payload.get("atm")
            step = SYMBOLS[matched_symbol].get("step", 50)
            if rows_objs and atm:
                context["open_positions"].append(analyze_open_position(pos, rows_objs, atm, step))
    except Exception as e:
        log.warning(f"Advisor context: position-gathering failed ({e}) -- continuing without it.")

    for symbol in SYMBOLS.keys():
        payload = state["last_payload_by_symbol"].get(symbol)
        is_live = bool(payload)
        as_of = None
        if not payload:
            fallback = _fetch_last_known_from_db(symbol)
            if not fallback:
                continue   # genuinely nothing ever logged for this symbol -- honestly omit it
            payload = {"ltp": fallback["ltp"], "pcr": fallback["pcr"], "atm": fallback["atm"], "rows": fallback["rows"]}
            as_of = fallback["as_of"]
            v1_levels = fallback["v1_levels"]
            v2_levels, v2_ts = {}, {}   # V2's in-memory state isn't DB-persisted -- honestly omit rather than guess
        else:
            v1_structure = state["market_structure_by_symbol"].get(symbol) or {}
            v1_levels = v1_structure.get("custom_levels") or {}
            v2 = state["v2_by_symbol"].get(symbol) or {}
            v2_ts = (v2.get("trend_and_signal") or {}) if v2 else {}
            v2_levels = v2.get("levels") or {}
        atm = payload.get("atm")
        payload_rows = payload.get("rows") or []
        step = SYMBOLS[symbol].get("step", 50)
        nearby = [r for r in payload_rows if r.get("strike") is not None and abs(r["strike"] - (atm or 0)) <= step * 3]
        ce_wall = max(nearby, key=lambda r: r.get("ce_oi") or 0, default=None) if nearby else None
        pe_wall = max(nearby, key=lambda r: r.get("pe_oi") or 0, default=None) if nearby else None
        today_price_action = _fetch_today_price_action(symbol)
        context["symbols_summary"].append({
            "symbol": symbol, "ltp": payload.get("ltp"), "pcr": payload.get("pcr"), "atm_strike": atm,
            "is_live": is_live, "as_of": as_of,
            "v1_resistance": v1_levels.get("resistance"), "v1_support": v1_levels.get("support"),
            "v1_resistance_reversal": v1_levels.get("resistance_reversal"), "v1_support_reversal": v1_levels.get("support_reversal"),
            "v2_resistance": v2_levels.get("resistance"), "v2_support": v2_levels.get("support"),
            "v2_trend": v2_ts.get("trend"), "v2_signal": v2_ts.get("signal"), "v2_signal_reason": v2_ts.get("reason"),
            "ce_oi_wall_strike": ce_wall.get("strike") if ce_wall else None, "ce_oi_wall_value": ce_wall.get("ce_oi") if ce_wall else None,
            "pe_oi_wall_strike": pe_wall.get("strike") if pe_wall else None, "pe_oi_wall_value": pe_wall.get("pe_oi") if pe_wall else None,
            "today_price_action": today_price_action,
        })

    try:
        from backtest import score_calibration_report
        calib = score_calibration_report(DB_PATH, min_sample=5)
        if calib.get("total_trades"):
            context["calibration"] = calib
    except Exception as e:
        log.warning(f"Advisor context: calibration-gathering failed ({e}) -- continuing without it.")

    return context


@app.route("/advisor")
@auth.roles_required("admin", "developer")
def advisor_page():
    """Advisory Chatbot page -- Q&A only, informational, cannot place/modify/close any trade."""
    from advisory_chatbot import CHATBOT_ENABLED
    return render_template("advisor.html", enabled=CHATBOT_ENABLED)


@app.route("/api/advisor-chat", methods=["POST"])
@auth.roles_required("admin", "developer")
def advisor_chat_api():
    from advisory_chatbot import ask_advisor
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    history = data.get("history") or []
    if not question:
        return jsonify({"error": "No question provided."}), 400
    if len(question) > 500:
        return jsonify({"error": "Question too long (max 500 characters)."}), 400
    context = _gather_advisor_context()
    result = ask_advisor(question, context, conversation_history=history)
    return jsonify(result)


@app.route("/live-positions")
@auth.roles_required("admin")
def live_positions_page():
    """Monitors CURRENTLY-OPEN Angel One positions (manually-placed trades
    included) -- read-only. Computes 1:2 target/SL and nearby OI-wall
    proximity for each, using the SAME live data already being tracked."""
    positions_analysis = []
    error = None
    try:
        angel_monitor = _get_position_monitor_angel()
        raw_positions = angel_monitor.get_open_positions()
    except Exception as e:
        raw_positions = []
        error = str(e)

    for pos in raw_positions:
        # Match this position's trading-symbol to one of OUR tracked
        # symbols (prefix-match, e.g. "NIFTY24JUL24000CE" -> "NIFTY") so we
        # can reuse its already-fetched live rows/atm/step for OI-wall analysis.
        matched_symbol = match_symbol_prefix(pos["symbol"])
        if not matched_symbol:
            positions_analysis.append({"symbol": pos["symbol"], "unmatched": True, "raw": pos})
            continue
        payload = state["last_payload_by_symbol"].get(matched_symbol) or {}
        payload_rows = payload.get("rows") or []
        rows_objs = [StrikeRow(**{k: v for k, v in r.items() if k in StrikeRow.__dataclass_fields__}) for r in payload_rows] if payload_rows else []
        atm = payload.get("atm")
        step = SYMBOLS[matched_symbol].get("step", 50)
        if rows_objs and atm:
            analysis = analyze_open_position(pos, rows_objs, atm, step)
            analysis["matched_symbol"] = matched_symbol
            positions_analysis.append(analysis)
        else:
            positions_analysis.append({"symbol": pos["symbol"], "matched_symbol": matched_symbol, "no_live_data": True, "raw": pos})

    return render_template("live_positions.html", positions=positions_analysis, error=error)


@app.route("/premarket-report")
@auth.roles_required("admin", "developer")
def premarket_report_page():
    """Pre-Market Prediction Report -- morning briefing across all symbols."""
    reports = []
    for symbol in SYMBOLS.keys():
        r = generate_premarket_report(symbol)
        if "error" not in r:
            reports.append(r)
    return render_template("premarket_report.html", reports=reports, symbol_count=len(SYMBOLS))


@app.route("/api/premarket-report/<symbol>")
@auth.roles_required("admin", "developer")
def premarket_report_api(symbol):
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    return jsonify(generate_premarket_report(symbol))


@app.route("/charts")
@auth.roles_required("admin", "developer")
def charts_page():
    """Multi-chart page -- PCR+Volume, CE/PE LTP, and combined charts, with
    ATM/ITM/OTM strike-selection and backdated (historical date) testing."""
    return render_template("charts.html", symbols=list(SYMBOLS.keys()))


@app.route("/charts-pro")
@auth.roles_required("admin", "developer")
def charts_pro_page():
    """Professional TradingView-style candlestick chart -- native 2D pan/zoom,
    price-scale drag, crosshair with OHLC snap (via Lightweight Charts,
    TradingView's own open-source charting library)."""
    return render_template("charts_pro.html", symbols=list(SYMBOLS.keys()))


def analyze_open_position(position, rows, atm, step):
    """
    Analysis for a MANUALLY-placed, currently-open Angel One position:
    - 1:2 Target/SL from entry-price, using the SAME 5%-max-risk cap already
      established for our own live-trading (MAX_SL_PERCENT) -- so a manual
      trade gets the SAME risk-discipline as our own signals, not a
      different/arbitrary rule.
    - Nearby OI-wall proximity: the largest CE/PE-OI strikes within a few
      steps of the current ATM, which act as likely support/resistance.
    Read-only -- does not place, modify, or close any order.
    """
    entry = position["buy_avg_price"]
    ltp = position["ltp"]
    is_long = position["net_qty"] > 0   # positive net-qty = bought (long) the option

    sl_distance = entry * MAX_SL_PERCENT
    target_distance = sl_distance * 2   # explicit 1:2 Risk:Reward, as requested
    if is_long:
        sl_price = round(entry - sl_distance, 2)
        target_price = round(entry + target_distance, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        target_price = round(entry - target_distance, 2)

    current_pnl_pct = round((ltp - entry) / entry * 100, 2) if entry else None
    if not is_long and entry:
        current_pnl_pct = round((entry - ltp) / entry * 100, 2)

    # Nearby OI-walls: strikes within +/-3 steps of ATM, ranked by OI size
    nearby = [r for r in rows if abs(r.strike - atm) <= step * 3]
    ce_wall = max(nearby, key=lambda r: r.ce_oi or 0, default=None) if nearby else None
    pe_wall = max(nearby, key=lambda r: r.pe_oi or 0, default=None) if nearby else None

    return {
        "symbol": position["symbol"], "product": position["product"],
        "direction": "LONG" if is_long else "SHORT",
        "entry_price": entry, "current_ltp": ltp, "current_pnl": position["pnl"],
        "current_pnl_pct": current_pnl_pct,
        "sl_price": sl_price, "target_price": target_price,
        "risk_reward": "1:2",
        "ce_wall_strike": ce_wall.strike if ce_wall else None, "ce_wall_oi": ce_wall.ce_oi if ce_wall else None,
        "pe_wall_strike": pe_wall.strike if pe_wall else None, "pe_wall_oi": pe_wall.pe_oi if pe_wall else None,
    }


def find_reversal_points(ltp_series, window=3):
    """
    Simple local-extrema detection: a point is a 'reversal point' if it's
    the highest (or lowest) value within `window` points on EITHER side --
    pure visualization of where price actually turned that day, not a new
    trading signal. Returns a list of {index, type: 'peak'|'trough', value}.
    """
    points = []
    n = len(ltp_series)
    for i in range(window, n - window):
        val = ltp_series[i]
        if val is None:
            continue
        neighborhood = [v for v in ltp_series[i - window:i + window + 1] if v is not None]
        if not neighborhood:
            continue
        if val == max(neighborhood) and val > ltp_series[i - window]:
            points.append({"index": i, "type": "peak", "value": val})
        elif val == min(neighborhood) and val < ltp_series[i - window]:
            points.append({"index": i, "type": "trough", "value": val})
    return points


@app.route("/api/ohlc-data/<symbol>")
@auth.roles_required("admin", "developer")
def ohlc_data_api(symbol):
    """
    Returns OHLC-bucketed candle data for candlestick charting:
    {candles: [{time, open, high, low, close}, ...],   -- underlying index
     ce_candles: [...], pe_candles: [...],               -- option premium OHLC
     volume: [{time, value, color}, ...],                -- CE+PE combined volume histogram
     pcr: [{time, value}, ...],
     v2_resistance, v2_support, date_used, strike_used}

    Query params:
      date         -- YYYY-MM-DD (default: today)
      moneyness    -- ATM, ITM1, ITM2, OTM1, OTM2 (default ATM)
      bucket_secs  -- candle width in seconds (default 60 = 1-minute bars)

    Aggregates from the SAME cycles/strikes tick-data as /api/chart-data --
    no new data-source, just bucketed differently for candlestick rendering.
    """
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    cfg = SYMBOLS[symbol]
    step = cfg.get("step", 50)
    date_str = request.args.get("date") or now_ist().date().isoformat()
    moneyness = request.args.get("moneyness", "ATM").upper()
    try:
        bucket_secs = max(15, int(request.args.get("bucket_secs", 60)))
    except ValueError:
        bucket_secs = 60

    offset_map = {"ATM": 0, "ITM1": -1, "ITM2": -2, "OTM1": 1, "OTM2": 2}
    offset = offset_map.get(moneyness, 0)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cycles = conn.execute(
        "SELECT id, ts, atm, pcr, underlying_ltp FROM cycles WHERE symbol=? AND date=? ORDER BY ts ASC",
        (symbol, date_str),
    ).fetchall()
    if not cycles:
        conn.close()
        return jsonify({"error": f"No logged data for {symbol} on {date_str}.",
                         "candles": [], "ce_candles": [], "pe_candles": [], "volume": [], "pcr": [],
                         "ce_oi": [], "pe_oi": [], "ce_oi_chg": [], "pe_oi_chg": [], "reversal_points": []})

    last_atm = cycles[-1]["atm"]
    target_strike = last_atm + offset * step if last_atm else None

    # Pull per-cycle CE/PE ltp+vol+oi at the target strike, aligned with cycle timestamps
    ticks = []
    for c in cycles:
        strike_row = conn.execute(
            "SELECT ce_ltp, pe_ltp, ce_vol, pe_vol, ce_oi, pe_oi, ce_oi_chg, pe_oi_chg FROM strikes WHERE cycle_id=? AND strike=?",
            (c["id"], target_strike),
        ).fetchone()
        if strike_row is None:
            continue
        try:
            epoch = dt.datetime.fromisoformat(c["ts"]).replace(tzinfo=IST).timestamp()
        except (ValueError, TypeError):
            continue
        ticks.append({
            "epoch": epoch, "underlying": c["underlying_ltp"], "pcr": c["pcr"],
            "ce_ltp": strike_row["ce_ltp"], "pe_ltp": strike_row["pe_ltp"],
            "ce_vol": strike_row["ce_vol"] or 0, "pe_vol": strike_row["pe_vol"] or 0,
            "ce_oi": strike_row["ce_oi"], "pe_oi": strike_row["pe_oi"],
            "ce_oi_chg": strike_row["ce_oi_chg"], "pe_oi_chg": strike_row["pe_oi_chg"],
        })

    # V2 resistance/support (same real-snapshot-based lookup as /api/chart-data)
    v2_resistance = v2_support = None
    snap_row = conn.execute(
        "SELECT custom_levels_json FROM market_structure_snapshots WHERE symbol=? AND date=? LIMIT 1",
        (symbol, date_str),
    ).fetchone()
    if snap_row and snap_row["custom_levels_json"]:
        try:
            levels = json.loads(snap_row["custom_levels_json"])
            if all(levels.get(k) for k in ("pdh", "pdl", "pdc")):
                v2_levels = compute_v2_levels(levels["pdh"], levels["pdl"], levels["pdc"], symbol=symbol)
                v2_resistance, v2_support = v2_levels["resistance"], v2_levels["support"]
        except (json.JSONDecodeError, TypeError):
            pass
    conn.close()

    def bucketize(ticks, value_key):
        """Groups ticks into bucket_secs-wide OHLC candles."""
        buckets = {}
        for t in ticks:
            if t[value_key] is None:
                continue
            bucket_ts = int(t["epoch"] // bucket_secs) * bucket_secs
            if bucket_ts not in buckets:
                buckets[bucket_ts] = {"time": bucket_ts, "open": t[value_key], "high": t[value_key],
                                       "low": t[value_key], "close": t[value_key]}
            b = buckets[bucket_ts]
            b["high"] = max(b["high"], t[value_key])
            b["low"] = min(b["low"], t[value_key])
            b["close"] = t[value_key]
        return [buckets[k] for k in sorted(buckets.keys())]

    def bucketize_sum(ticks, ce_key, pe_key):
        """Groups ticks into volume-histogram bars (CE+PE summed per bucket)."""
        buckets = {}
        for t in ticks:
            bucket_ts = int(t["epoch"] // bucket_secs) * bucket_secs
            if bucket_ts not in buckets:
                buckets[bucket_ts] = {"time": bucket_ts, "ce": 0, "pe": 0}
            buckets[bucket_ts]["ce"] += (t[ce_key] or 0)
            buckets[bucket_ts]["pe"] += (t[pe_key] or 0)
        result = []
        for k in sorted(buckets.keys()):
            b = buckets[k]
            result.append({"time": k, "value": b["ce"] + b["pe"], "ce": b["ce"], "pe": b["pe"]})
        return result

    def bucketize_last(ticks, value_key):
        """Groups ticks and takes the LAST value per bucket (for PCR line)."""
        buckets = {}
        for t in ticks:
            if t[value_key] is None:
                continue
            bucket_ts = int(t["epoch"] // bucket_secs) * bucket_secs
            buckets[bucket_ts] = t[value_key]
        return [{"time": k, "value": buckets[k]} for k in sorted(buckets.keys())]

    underlying_candles = bucketize(ticks, "underlying")
    underlying_closes = [c["close"] for c in underlying_candles]
    reversal_indices = find_reversal_points(underlying_closes)
    reversal_points = [
        {"time": underlying_candles[p["index"]]["time"], "value": p["value"], "type": p["type"]}
        for p in reversal_indices
    ]
    detected_patterns = detect_patterns(underlying_candles)

    return jsonify({
        "candles": underlying_candles,
        "ce_candles": bucketize(ticks, "ce_ltp"),
        "pe_candles": bucketize(ticks, "pe_ltp"),
        "volume": bucketize_sum(ticks, "ce_vol", "pe_vol"),
        "pcr": bucketize_last(ticks, "pcr"),
        "ce_oi": bucketize_last(ticks, "ce_oi"),
        "pe_oi": bucketize_last(ticks, "pe_oi"),
        "ce_oi_chg": bucketize_last(ticks, "ce_oi_chg"),
        "pe_oi_chg": bucketize_last(ticks, "pe_oi_chg"),
        "v2_resistance": v2_resistance, "v2_support": v2_support,
        "date_used": date_str, "strike_used": target_strike, "moneyness": moneyness,
        "bucket_secs": bucket_secs, "reversal_points": reversal_points,
        "candlestick_patterns": detected_patterns,
    })


@app.route("/api/chart-data/<symbol>")
@auth.roles_required("admin", "developer")
def chart_data_api(symbol):
    """
    Returns time-series data for the requested symbol/date/moneyness:
    {time[], pcr[], ce_ltp[], pe_ltp[], ce_vol[], pe_vol[], underlying_ltp[],
     ce_oi[], pe_oi[], ce_oi_chg[], pe_oi_chg[], reversal_points[],
     v2_resistance, v2_support, strike_used, date_used}

    Query params:
      date       -- YYYY-MM-DD (default: today) -- for backdated/historical testing
      moneyness  -- ATM, ITM1, ITM2, OTM1, OTM2 (default ATM)

    Strike is anchored to the LAST cycle's ATM in the requested date's data
    (not re-computed per-cycle) -- avoids a discontinuous series if ATM
    shifted intraday, at the cost of the strike being slightly-off from
    "true ATM" at the start of the day if price moved a lot. Good enough
    for charting; not used for any trading decision.

    v2_resistance/v2_support are SINGLE values (the whole day's Engine V2
    levels, from that day's real PDH/PDL snapshot) -- meant to be drawn as
    horizontal reference lines, not a time-series.

    reversal_points are simple local-extrema in the underlying LTP series
    (points where price turned) -- pure visualization of what ACTUALLY
    happened that day, not a new trading signal.
    """
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    cfg = SYMBOLS[symbol]
    step = cfg.get("step", 50)
    date_str = request.args.get("date") or now_ist().date().isoformat()
    moneyness = request.args.get("moneyness", "ATM").upper()

    offset_map = {"ATM": 0, "ITM1": -1, "ITM2": -2, "OTM1": 1, "OTM2": 2}
    offset = offset_map.get(moneyness, 0)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cycles = conn.execute(
        "SELECT id, time, ts, atm, pcr, underlying_ltp FROM cycles WHERE symbol=? AND date=? ORDER BY ts ASC",
        (symbol, date_str),
    ).fetchall()
    if not cycles:
        conn.close()
        return jsonify({"error": f"No logged data for {symbol} on {date_str}.", "time": [], "pcr": [],
                         "ce_ltp": [], "pe_ltp": [], "ce_vol": [], "pe_vol": [], "underlying_ltp": [],
                         "ce_oi": [], "pe_oi": [], "ce_oi_chg": [], "pe_oi_chg": [], "reversal_points": []})

    last_atm = cycles[-1]["atm"]
    target_strike = last_atm + offset * step if last_atm else None

    # Engine V2's resistance/support for this day (single values, real snapshot only)
    v2_resistance = v2_support = None
    snap_row = conn.execute(
        "SELECT custom_levels_json FROM market_structure_snapshots WHERE symbol=? AND date=? LIMIT 1",
        (symbol, date_str),
    ).fetchone()
    if snap_row and snap_row["custom_levels_json"]:
        try:
            levels = json.loads(snap_row["custom_levels_json"])
            if all(levels.get(k) for k in ("pdh", "pdl", "pdc")):
                v2_levels = compute_v2_levels(levels["pdh"], levels["pdl"], levels["pdc"], symbol=symbol)
                v2_resistance, v2_support = v2_levels["resistance"], v2_levels["support"]
        except (json.JSONDecodeError, TypeError):
            pass

    result = {"time": [], "pcr": [], "ce_ltp": [], "pe_ltp": [], "ce_vol": [], "pe_vol": [],
              "underlying_ltp": [], "ce_oi": [], "pe_oi": [], "ce_oi_chg": [], "pe_oi_chg": [],
              "strike_used": target_strike, "date_used": date_str, "moneyness": moneyness,
              "v2_resistance": v2_resistance, "v2_support": v2_support}

    for c in cycles:
        strike_row = conn.execute(
            "SELECT ce_ltp, pe_ltp, ce_vol, pe_vol, ce_oi, pe_oi, ce_oi_chg, pe_oi_chg "
            "FROM strikes WHERE cycle_id=? AND strike=?",
            (c["id"], target_strike),
        ).fetchone()
        if strike_row is None:
            continue
        result["time"].append(c["time"])
        result["pcr"].append(c["pcr"])
        result["underlying_ltp"].append(c["underlying_ltp"])
        result["ce_ltp"].append(strike_row["ce_ltp"])
        result["pe_ltp"].append(strike_row["pe_ltp"])
        result["ce_vol"].append(strike_row["ce_vol"])
        result["pe_vol"].append(strike_row["pe_vol"])
        result["ce_oi"].append(strike_row["ce_oi"])
        result["pe_oi"].append(strike_row["pe_oi"])
        result["ce_oi_chg"].append(strike_row["ce_oi_chg"])
        result["pe_oi_chg"].append(strike_row["pe_oi_chg"])
    conn.close()

    # Simple local-extrema (reversal-point) detection on the underlying LTP series
    result["reversal_points"] = find_reversal_points(result["underlying_ltp"])

    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/engine-v2")
@auth.roles_required("admin", "developer")
def engine_v2_page():
    """Engine V2 tab -- read-only comparison view. Shows V2's levels/
    probabilities/trend/signal next to V1's, plus live PCR/Volume/LTP,
    for symbols where V2 is enabled. Reuses already-fetched live data
    (no duplicate API calls)."""
    rows = []
    for symbol in SYMBOLS.keys():
        v2 = state["v2_by_symbol"].get(symbol)
        v1_structure = state["market_structure_by_symbol"].get(symbol) or {}
        v1_levels = v1_structure.get("custom_levels") or {}
        payload = state["last_payload_by_symbol"].get(symbol) or {}
        payload_rows = payload.get("rows") or []
        rows.append({
            "symbol": symbol,
            "enabled": state["engine_v2_enabled"].get(symbol, False),
            "v2": v2,
            "v1_resistance": v1_levels.get("resistance"), "v1_support": v1_levels.get("support"),
            "ltp": payload.get("ltp"), "pcr": payload.get("pcr"), "updated": payload.get("updated"),
            "ce_volume": sum(r.get("ce_vol") or 0 for r in payload_rows),
            "pe_volume": sum(r.get("pe_vol") or 0 for r in payload_rows),
        })
    return render_template("engine_v2.html", rows=rows, symbol_count=len(SYMBOLS))


@app.route("/smart-analysis")
@auth.roles_required("admin", "developer")
def smart_analysis_page():
    """
    Smart Dashboard: cross-symbol comparison table --
    OI-wall S/R vs PDH-formula S/R (do the two independently-computed
    systems agree?), reversal/breakout state, pivot, max pain, and
    time-lagged OI (30min) / Volume (40min) comparison -- all from
    genuinely logged historical data, never invented.
    """
    rows = []
    for symbol in SYMBOLS.keys():
        analysis = compute_smart_analysis(symbol)
        if analysis:
            rows.append(analysis)
    return render_template("smart_analysis.html", rows=rows, symbol_count=len(SYMBOLS))


@app.route("/api/smart-analysis/<symbol>")
@auth.roles_required("admin", "developer")
def smart_analysis_api(symbol):
    """JSON endpoint for a single symbol's smart-analysis -- used by the
    chatbot and available for polling/auto-refresh on the Smart Dashboard page."""
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    analysis = compute_smart_analysis(symbol)
    return jsonify(analysis or {"error": "No live data yet for this symbol."})


@app.route("/api/scalp/<symbol>")
@auth.roles_required("admin", "developer")
def scalp_signal_api(symbol):
    """JSON endpoint for the Scalping Engine's latest evaluation PLUS its own
    separate paper-trading track record (EXPERIMENTAL, ADVISORY signal --
    but the paper trades themselves are genuinely opened/tracked, see
    update_scalp_paper_trading). Enable via /dev-settings first
    (scalp_engine_enabled, OFF by default); until then this just reports
    that it's disabled rather than silently returning stale/empty data.

    NOTE (2026-08-01): response shape changed from a bare {"CE":.., "PE":..}
    to {"signal": {...}, "paper": {...}} to make room for the paper-trading
    stats -- no external consumers existed yet at the time of this change."""
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    if not state.get("scalp_engine_enabled"):
        return jsonify({"error": "Scalping Engine is disabled -- enable it on /dev-settings first.",
                         "enabled": False}), 400
    signal = state["scalp_signal_by_symbol"].get(symbol)
    if not signal:
        return jsonify({"error": "No live scalp evaluation yet for this symbol.", "enabled": True})
    bucket = state["scalp_paper_by_symbol"].get(symbol)
    total_trades = (bucket["wins"] + bucket["losses"] + bucket["time_exits"]) if bucket else 0
    paper = {
        "open_trade": ({k: v for k, v in bucket["open_trade"].items() if k != "entry_time_obj"}
                        if bucket and bucket["open_trade"] else None),
        "history": ([{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]]
                     if bucket else []),
        "wins": bucket["wins"] if bucket else 0, "losses": bucket["losses"] if bucket else 0,
        "time_exits": bucket["time_exits"] if bucket else 0,
        "win_rate": round(bucket["wins"] / total_trades * 100, 1) if bucket and total_trades else 0.0,
        "total_points": round(bucket["total_points"], 2) if bucket else 0.0,
    }
    return jsonify({"signal": signal, "paper": paper, "enabled": True})


@app.route("/engine-v3")
@auth.roles_required("admin", "developer")
def engine_v3_page():
    """S/R Engine V3 tab -- read-only summary across all symbols: Dynamic S/R,
    hold/break probability, confidence, previous-day validation status,
    today's Held/Broke/Extended/Flipped outcome, trade decision, and V3's own
    separate paper-trading win-rate. Reuses already-fetched live data (no
    duplicate API calls), same pattern as engine_v2_page."""
    rows = []
    for symbol in SYMBOLS.keys():
        v3 = state["v3_signal_by_symbol"].get(symbol)
        bucket = state["v3_paper_by_symbol"].get(symbol)
        total_trades = (bucket["wins"] + bucket["losses"] + bucket["time_exits"]) if bucket else 0
        payload = state["last_payload_by_symbol"].get(symbol) or {}

        # Derived HELD/BROKE/EXTENDED/FLIPPED display label (per spec) --
        # classify_level_outcome() itself only distinguishes HELD/BROKE/FLIPPED/
        # UNKNOWN (a single-purpose price-interaction check); "EXTENDED" is
        # BROKE + the separate weighted-evidence extension judgment agreeing,
        # kept as two distinct functions in sr_engine_v3.py (facts vs judgment)
        # and combined here only for display.
        outcome_display = {"support": None, "resistance": None}
        if v3:
            raw_outcome = v3.get("today_outcome") or {}
            if raw_outcome.get("support") == "BROKE" and (v3.get("support_extend_down") or {}).get("extending"):
                outcome_display["support"] = "EXTENDED"
            else:
                outcome_display["support"] = raw_outcome.get("support")
            if raw_outcome.get("resistance") == "BROKE" and (v3.get("resistance_extend_up") or {}).get("extending"):
                outcome_display["resistance"] = "EXTENDED"
            else:
                outcome_display["resistance"] = raw_outcome.get("resistance")

        rows.append({
            "symbol": symbol,
            "enabled": state["v3_engine_enabled"].get(symbol, False),
            "v3": v3,
            "outcome_display": outcome_display,
            "ltp": payload.get("ltp"), "pcr": payload.get("pcr"), "updated": payload.get("updated"),
            "win_rate": round(bucket["wins"] / total_trades * 100, 1) if bucket and total_trades else 0.0,
            "total_points": round(bucket["total_points"], 2) if bucket else 0.0,
            "wins": bucket["wins"] if bucket else 0, "losses": bucket["losses"] if bucket else 0,
            "time_exits": bucket["time_exits"] if bucket else 0,
        })
    return render_template("engine_v3.html", rows=rows, symbol_count=len(SYMBOLS))


@app.route("/api/v3/<symbol>")
@auth.roles_required("admin", "developer")
def v3_signal_api(symbol):
    """JSON endpoint for S/R Engine V3's latest evaluation PLUS its own
    separate paper-trading track record (EXPERIMENTAL signal -- but the
    paper trades themselves are genuinely opened/tracked, see
    update_v3_paper_trading). Enable via /dev-settings first
    (per-symbol, OFF by default); until then this just reports that it's
    disabled rather than silently returning stale/empty data. Same response
    shape convention as /api/scalp/<symbol>."""
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    if not state["v3_engine_enabled"].get(symbol):
        return jsonify({"error": "S/R Engine V3 is disabled for this symbol -- enable it on /dev-settings first.",
                         "enabled": False}), 400
    signal = state["v3_signal_by_symbol"].get(symbol)
    if not signal:
        return jsonify({"error": "No live V3 evaluation yet for this symbol.", "enabled": True})
    bucket = state["v3_paper_by_symbol"].get(symbol)
    total_trades = (bucket["wins"] + bucket["losses"] + bucket["time_exits"]) if bucket else 0
    paper = {
        "open_trade": ({k: v for k, v in bucket["open_trade"].items() if k != "entry_time_obj"}
                        if bucket and bucket["open_trade"] else None),
        "history": ([{k: v for k, v in t.items() if k != "entry_time_obj"} for t in list(bucket["history"])[:15]]
                     if bucket else []),
        "wins": bucket["wins"] if bucket else 0, "losses": bucket["losses"] if bucket else 0,
        "time_exits": bucket["time_exits"] if bucket else 0,
        "win_rate": round(bucket["wins"] / total_trades * 100, 1) if bucket and total_trades else 0.0,
        "total_points": round(bucket["total_points"], 2) if bucket else 0.0,
    }
    return jsonify({"signal": signal, "paper": paper, "enabled": True})


@app.route("/dynamic-sr")
@auth.roles_required("admin", "developer")
def dynamic_sr_page():
    """Dynamic S/R Engine (V1) tab -- PDH/PDL-based resistance/support ladder
    that extends without limit as price moves beyond it, plus BUY/SELL
    signals with SL/targets/confidence. Pure price/volume (no OI/PCR/strike
    selection -- that's still the existing OI-based engines' job; this reuses
    already-fetched candles, no duplicate API calls), same pattern as
    engine_v3_page."""
    rows = []
    for symbol in SYMBOLS.keys():
        dsr = state["dynamic_sr_by_symbol"].get(symbol)
        payload = state["last_payload_by_symbol"].get(symbol) or {}
        rows.append({"symbol": symbol, "dsr": dsr, "ltp": payload.get("ltp"), "updated": payload.get("updated")})
    return render_template("dynamic_sr.html", rows=rows, symbol_count=len(SYMBOLS))


@app.route("/api/dynamic-sr/<symbol>")
@auth.roles_required("admin", "developer")
def dynamic_sr_api(symbol):
    """JSON endpoint for the Dynamic S/R Engine's latest evaluation. Same
    response-shape convention as /api/v3/<symbol>."""
    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 400
    dsr = state["dynamic_sr_by_symbol"].get(symbol)
    if not dsr:
        return jsonify({"error": "No live Dynamic S/R evaluation yet for this symbol."})
    return jsonify(dsr)


@socketio.on("connect")
def on_connect():
    # The Basic-Auth before_request guard used to cover this handshake too
    # (see the removed _global_auth_guard) -- now that access control is
    # session-based, this handler does its own check against the SAME signed
    # session cookie (Flask-SocketIO exposes it here since the handshake is a
    # same-origin HTTP request carrying the browser's cookies). Returning
    # False rejects the connection outright.
    user_id = session.get("user_id")
    if user_id is None:
        log.info("Rejected Socket.IO connection: no session.")
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if user is None or user["is_suspended"]:
        log.info(f"Rejected Socket.IO connection: user_id={user_id} missing/suspended.")
        return False
    if user["role"] == "subscriber" and not auth.subscription_is_active(user):
        log.info(f"Rejected Socket.IO connection: user_id={user_id} trial/subscription lapsed.")
        return False
    log.info(f"Browser client connected (user_id={user_id}, role={user['role']}).")
    # Private per-user room -- manual-trade auto-exit/limit-fill alerts emit
    # here (room=f"user_{user_id}"), NEVER to room=symbol, which would
    # broadcast one user's trade activity to every other user watching the
    # same symbol. No on_disconnect cleanup needed -- Flask-SocketIO drops a
    # sid from all its rooms automatically on disconnect.
    join_room(f"user_{user_id}", sid=request.sid)
    if state.get("market_status") and state["market_status"].get("open") is False:
        socketio.emit("market_status", state["market_status"], to=request.sid)


def _leave_current_symbol_room(sid):
    prev_symbol = state["sid_symbol"].pop(sid, None)
    if prev_symbol:
        leave_room(prev_symbol, sid=sid)
        state["symbol_viewers"].get(prev_symbol, set()).discard(sid)


@socketio.on("switch_symbol")
def on_switch_symbol(data):
    symbol = (data or {}).get("symbol", "").upper()
    if symbol in SYMBOLS:
        sid = request.sid
        _leave_current_symbol_room(sid)   # a client watches exactly one symbol's room at a time
        join_room(symbol, sid=sid)
        state["sid_symbol"][sid] = symbol
        state["symbol_viewers"].setdefault(symbol, set()).add(sid)
        log.info(f"Client {sid} switched to symbol -> {symbol}")
        cached = state["last_payload_by_symbol"].get(symbol)
        if cached:
            socketio.emit("update", cached, to=sid)
        ms = state["market_status_by_symbol"].get(symbol)
        if ms and ms.get("open") is False:
            state["market_status"] = ms
            socketio.emit("market_status", ms, to=sid)
    else:
        log.warning(f"Ignored switch_symbol request for unknown symbol: {symbol}")


@socketio.on("disconnect")
def on_disconnect():
    _leave_current_symbol_room(request.sid)


# ----------------------------------------------------------------------------
# Startup -- runs on import, NOT just under __main__, so this works whether
# launched directly (`python3 app.py`, Termux) or via a production WSGI
# server (`gunicorn app:app`, VPS -- see oi-dashboard.service). Do not move
# this back inside the __main__ guard, or gunicorn deployments will silently
# never fetch any data (the module gets imported, __main__ never runs).
# ----------------------------------------------------------------------------
_verify_all_routes_protected()   # fail-closed self-check -- runs always, not just live-mode

if not os.getenv("SKIP_AUTOSTART"):
    init_db()
    load_paper_state_from_db()
    load_scalp_paper_state_from_db()
    load_v3_paper_state_from_db()
    mcx_session_config.warn_if_approximate()   # Milestone 12, Phase 2C: MCX seasonal-close cutover dates are not yet exchange-circular-verified
    start_all_symbol_loops()
    log.info("Background data-fetch loop started.")
    # Milestone 12, Phase 1: OFF by default (agents.config.
    # RUNTIME_SCHEDULER_ENABLED) -- see agents/runtime/lifecycle.py's own
    # docstring for the full activation contract. Never raises; a
    # disabled flag or a lost singleton-lock race both degrade to a
    # logged no-op, never a startup crash.
    if runtime_lifecycle.start_scheduler_background(task_starter=socketio.start_background_task):
        log.info("Runtime scheduler activated (Milestone 12, Phase 1).")
    # Milestone 14, Phase 3: the dashboard's live-trading badge must
    # always start in PAPER MODE, regardless of whatever an admin had it
    # set to before the last restart -- see trading_mode.py's own
    # module docstring for why this is label/audit-only (no code path
    # this reset touches can ever place a real broker order).
    runtime_trading_mode.reset_to_paper_on_boot()
    log.info(f"Trading mode reset to PAPER on boot (badge: {runtime_trading_mode.BADGE[runtime_trading_mode.PAPER]}).")


if __name__ == "__main__":
    log.info(f"Starting Flask-SocketIO dashboard on 0.0.0.0:{PORT} (direct run mode)")
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
