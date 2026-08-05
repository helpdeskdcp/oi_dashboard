#!/usr/bin/env python3
"""
nse_fetcher.py -- Production-grade NSE option chain client.

METHOD USED (as verified by user, 2026-07-11): NSE Next.js frontend JavaScript
bundle inspection. Discovered the current internal GetQuoteApi option-chain
endpoints, established an NSE cookie session, fetched the expiry dropdown,
then fetched and parsed the live option-chain JSON.

This is NOT an official documented NSE market-data API -- it's the same
internal endpoint NSE's own website frontend calls. NSE's terms and
data-usage restrictions apply, and this internal endpoint can change without
notice (it already changed once: the old /api/option-chain-indices path
started returning Akamai-blocked 404s; this GetQuoteApi path was discovered
as the current replacement). Treat this as best-effort, not guaranteed.

VERIFIED ENDPOINTS (do not change without re-verifying against the live site):
  Dropdown : GET /api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainDropdown&symbol={SYMBOL}
  Chain    : GET /api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol={SYMBOL}&params=expiryDate%3D{DD-Mon-YYYY}

NOT YET VERIFIED -- do not use in production:
  wss://streamer.nseindia.com/streams/fo/mbp?symbol=...&expiry=...&strike=...
  (found in the JS bundle but never independently tested; kept out of this
  module on purpose until someone verifies it actually connects and streams).
"""

import os
import time
import json
import logging
import datetime as dt

import requests

log = logging.getLogger("nse_fetcher")

_IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def _now_ist():
    """UTC-safe IST 'now' -- dt.datetime.now() alone follows the server OS
    timezone, which drifts the trading day boundary on a UTC-clocked VPS
    (e.g. after 18:30 UTC it's already the next day in IST)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + _IST_OFFSET


class NSECircuitBreakerOpen(Exception):
    """Raised when too many consecutive failures have tripped the breaker."""
    pass


class NSEFetcher:
    BASE = "https://www.nseindia.com"
    ENDPOINT = "/api/NextApi/apiClient/GetQuoteApi"

    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36"),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/get-quote/optionchain/NIFTY/NIFTY-50",
    }

    def __init__(self, snapshot_dir=None, max_retries=3, backoff_base=1.6,
                 circuit_breaker_threshold=5, circuit_breaker_cooldown=120,
                 cookie_refresh_interval=900, stale_seconds=60,
                 expiry_cache_seconds=3600):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

        self.snapshot_dir = snapshot_dir
        if snapshot_dir:
            os.makedirs(snapshot_dir, exist_ok=True)

        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.cookie_refresh_interval = cookie_refresh_interval
        self.stale_seconds = stale_seconds
        self.expiry_cache_seconds = expiry_cache_seconds

        self._cookies_set_at = None
        self._consecutive_failures = 0
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_cooldown = circuit_breaker_cooldown
        self._circuit_open_until = None

        self._last_success_at = {}     # {symbol: datetime}
        self._expiry_cache = {}        # {symbol: (expiry_str, fetched_at)}

        self._bootstrap_session()

    # ---------------------------------------------------------------- session

    def _bootstrap_session(self):
        try:
            self.session.get(self.BASE, timeout=8)
            self._cookies_set_at = time.time()
            log.info("NSE session cookies refreshed.")
            return True
        except Exception as e:
            log.warning(f"NSE session bootstrap failed: {e}")
            return False

    def _ensure_fresh_session(self):
        if self._cookies_set_at is None or (time.time() - self._cookies_set_at) > self.cookie_refresh_interval:
            self._bootstrap_session()

    # ------------------------------------------------------- circuit breaker

    def _circuit_open(self):
        return self._circuit_open_until is not None and time.time() < self._circuit_open_until

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_threshold:
            self._circuit_open_until = time.time() + self._circuit_breaker_cooldown
            log.warning(
                f"NSE circuit breaker OPEN for {self._circuit_breaker_cooldown}s "
                f"after {self._consecutive_failures} consecutive failures."
            )

    def _record_success(self):
        self._consecutive_failures = 0
        self._circuit_open_until = None

    # --------------------------------------------------- low-level request

    def _get_json(self, params, snapshot_name=None, validate=None):
        """validate: optional callable(data) that raises if the payload is
        malformed/empty -- run BEFORE _record_success(), so a persistent
        "200 OK but empty body" failure mode still trips the circuit breaker
        instead of looking like a success on every attempt."""
        if self._circuit_open():
            reopen_at = dt.datetime.fromtimestamp(self._circuit_open_until).strftime("%H:%M:%S")
            raise NSECircuitBreakerOpen(f"NSE circuit breaker open until {reopen_at}")

        self._ensure_fresh_session()
        last_exc = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(f"{self.BASE}{self.ENDPOINT}", params=params, timeout=8)
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                if "json" not in ctype.lower():
                    raise RuntimeError(f"Unexpected content-type: {ctype!r} (body starts: {resp.text[:120]!r})")
                data = resp.json()
                if validate:
                    validate(data)
                self._record_success()
                if snapshot_name:
                    self._save_snapshot(snapshot_name, resp.content)
                return data
            except Exception as e:
                last_exc = e
                log.warning(f"NSE request attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base ** attempt)
                    if attempt == 2:
                        self._bootstrap_session()   # cookies may have expired mid-retry

        self._record_failure()
        raise last_exc

    def _save_snapshot(self, name, raw_bytes):
        if not self.snapshot_dir:
            return
        try:
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.snapshot_dir, f"{name}_{ts}.json")
            with open(path, "wb") as f:
                f.write(raw_bytes)
        except Exception as e:
            log.warning(f"NSE snapshot save failed: {e}")

    # -------------------------------------------------------------- public

    def get_expiry_dropdown(self, symbol):
        def _validate(d):
            if "expiryDates" not in d:
                raise ValueError(f"Malformed dropdown response for {symbol}: missing 'expiryDates'")
        data = self._get_json(
            {"functionName": "getOptionChainDropdown", "symbol": symbol},
            snapshot_name=f"dropdown_{symbol}",
            validate=_validate,
        )
        return data["expiryDates"]

    def get_nearest_expiry(self, symbol, force_refresh=False):
        cached = self._expiry_cache.get(symbol)
        if not force_refresh and cached and (time.time() - cached[1]) < self.expiry_cache_seconds:
            return cached[0]

        expiries = self.get_expiry_dropdown(symbol)
        today = _now_ist().date()
        parsed = []
        for e in expiries:
            try:
                d = dt.datetime.strptime(e, "%d-%b-%Y").date()
                if d >= today:
                    parsed.append((d, e))
            except ValueError:
                continue   # skip anything that isn't a real expiry string (defensive)

        if not parsed:
            raise ValueError(f"No valid upcoming expiry found for {symbol} in {expiries}")

        parsed.sort(key=lambda x: x[0])
        nearest = parsed[0][1]
        self._expiry_cache[symbol] = (nearest, time.time())
        return nearest

    def get_option_chain(self, symbol, expiry=None):
        """Returns (raw_json, expiry_used). Raises on failure -- caller decides fallback behavior."""
        if not expiry:
            expiry = self.get_nearest_expiry(symbol)

        def _validate(d):
            if "data" not in d or not d["data"]:
                raise ValueError(f"Malformed/empty option-chain response for {symbol}")
        data = self._get_json(
            {"functionName": "getOptionChainData", "symbol": symbol, "params": f"expiryDate={expiry}"},
            snapshot_name=f"chain_{symbol}",
            validate=_validate,
        )

        self._last_success_at[symbol] = _now_ist()
        return data, expiry

    def is_stale(self, symbol):
        last = self._last_success_at.get(symbol)
        if not last:
            return True
        return (_now_ist() - last).total_seconds() > self.stale_seconds


# ------------------------------------------------------------------------
# Normalization -- converts NSE's raw JSON into the same StrikeRow shape
# oi_engine.py uses, so the SAME PCR/Max Pain/OI-wall/bias logic can run on
# either Angel One or NSE data.
# ------------------------------------------------------------------------

def normalize_nse_chain(chain_json, StrikeRowCls, classify_buildup_fn, wanted_strikes=None):
    """
    NSE's changeinOpenInterest is a DAY-level change (vs previous close), not a
    cycle-level delta like our Angel One tracking -- so classify_buildup here
    reflects the day's overall buildup, a genuinely different (complementary)
    signal to Angel's intraday cycle-to-cycle read.
    """
    rows_raw = chain_json.get("data", [])
    underlying = None
    for r in rows_raw:
        ce, pe = r.get("CE") or {}, r.get("PE") or {}
        underlying = ce.get("underlyingValue") or pe.get("underlyingValue")
        if underlying:
            break
    if not underlying:
        raise ValueError("Could not determine underlyingValue from NSE response")

    by_strike = {}
    for r in rows_raw:
        sp = r.get("strikePrice")
        if wanted_strikes and sp not in wanted_strikes:
            continue
        ce, pe = r.get("CE") or {}, r.get("PE") or {}
        row = StrikeRowCls(strike=sp)
        row.ce_oi = ce.get("openInterest", 0)
        row.ce_oi_chg = ce.get("changeinOpenInterest", 0)
        row.ce_vol = ce.get("totalTradedVolume", 0)
        row.ce_ltp = ce.get("lastPrice", 0.0)
        row.ce_chg_pct = ce.get("pChange", 0.0)
        row.ce_iv = ce.get("impliedVolatility", 0.0)
        row.ce_buy_qty = ce.get("totalBuyQuantity", 0)
        row.ce_sell_qty = ce.get("totalSellQuantity", 0)
        row.pe_oi = pe.get("openInterest", 0)
        row.pe_oi_chg = pe.get("changeinOpenInterest", 0)
        row.pe_vol = pe.get("totalTradedVolume", 0)
        row.pe_ltp = pe.get("lastPrice", 0.0)
        row.pe_chg_pct = pe.get("pChange", 0.0)
        row.pe_iv = pe.get("impliedVolatility", 0.0)
        row.pe_buy_qty = pe.get("totalBuyQuantity", 0)
        row.pe_sell_qty = pe.get("totalSellQuantity", 0)
        row.ce_signal = classify_buildup_fn(row.ce_chg_pct, row.ce_oi_chg)
        row.pe_signal = classify_buildup_fn(row.pe_chg_pct, row.pe_oi_chg)
        by_strike[sp] = row

    return by_strike, underlying


def find_atm(underlying, step):
    return int(round(underlying / step) * step)


# ------------------------------------------------------------------------
# CLI self-test -- fetch NIFTY (or any symbol passed as argv[1]), run it
# through oi_engine's PCR/Max Pain/S-R/bias, print a table.
# ------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from oi_engine import StrikeRow, classify_buildup, calc_pcr, calc_max_pain, oi_walls, detect_bias

    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY"
    step = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}.get(symbol, 50)
    strikes_each_side = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    fetcher = NSEFetcher(snapshot_dir="nse_snapshots")

    try:
        expiry = fetcher.get_nearest_expiry(symbol)
        print(f"Nearest expiry for {symbol}: {expiry}")

        chain_json, used_expiry = fetcher.get_option_chain(symbol, expiry)
        rows_by_strike, underlying = normalize_nse_chain(chain_json, StrikeRow, classify_buildup)

        atm = find_atm(underlying, step)
        wanted = sorted(set(
            [atm] +
            [atm + i * step for i in range(1, strikes_each_side + 1)] +
            [atm - i * step for i in range(1, strikes_each_side + 1)]
        ))
        rows = [rows_by_strike[s] for s in wanted if s in rows_by_strike]

        pcr = calc_pcr(rows)
        max_pain = calc_max_pain(rows)
        support, resistance = oi_walls(rows)
        bias, note = detect_bias(rows, atm, pcr)

        print(f"\n{symbol} SPOT: {underlying}  |  ATM: {atm}  |  Expiry: {used_expiry}")
        print(f"PCR: {pcr}  |  Max Pain: {max_pain}  |  Bias: {bias}")
        print(f"Note: {note}")
        print(f"Resistance walls: {[r.strike for r in resistance]}")
        print(f"Support walls: {[s.strike for s in support]}\n")

        header = f"{'CE OI':>10} {'CE ChgOI':>10} {'CE Vol':>10} {'CE LTP':>9} {'CE Sig':>16} {'STRIKE':>8} {'PE Sig':>16} {'PE LTP':>9} {'PE Vol':>10} {'PE ChgOI':>10} {'PE OI':>10}"
        print(header)
        print("-" * len(header))
        for r in rows:
            marker = " <ATM>" if r.strike == atm else ""
            print(f"{r.ce_oi:>10} {r.ce_oi_chg:>10} {r.ce_vol:>10} {r.ce_ltp:>9.2f} {r.ce_signal:>16} "
                  f"{r.strike:>8}{marker:<6} {r.pe_signal:>16} {r.pe_ltp:>9.2f} {r.pe_vol:>10} {r.pe_oi_chg:>10} {r.pe_oi:>10}")

    except NSECircuitBreakerOpen as e:
        print(f"Circuit breaker is open: {e}")
    except Exception as e:
        print(f"NSE fetch failed: {e}")
