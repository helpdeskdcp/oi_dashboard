#!/usr/bin/env python3
"""
bse_fetcher.py -- BSE option chain fetcher (SENSEX, BANKEX, SX50, BSE FOCUSED IT).

NSE's endpoint only covers NSE indices (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY) --
it has no SENSEX data at all, since SENSEX trades on BSE. This module talks to
BSE's own public API directly, verified working by the user (2026-07-12):

  https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry_IV/w        (expiry list)
  https://api.bseindia.com/BseIndiaAPI/api/DerivOptionChain_IV/w (option chain)

Like nse_fetcher.py, this is undocumented/internal-style usage of a public
exchange API -- BSE's own website calls it, but it's not a published
data-vendor product. Treat it the same way: secondary cross-check, not primary.
"""

from __future__ import annotations

import math
import time
import logging
import datetime as dt
import requests
from typing import Any, Dict, List, Optional

log = logging.getLogger("bse_fetcher")

_IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def _now_ist():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + _IST_OFFSET


def _parse_bse_expiry(expiry_str) -> dt.date:
    """BSE's expiry-date string format isn't documented and may vary across
    payloads -- try the known candidates; unparseable sinks to date.max so it
    can never be mistakenly picked as 'nearest' (mirrors app.py's
    parse_expiry() for Angel One's DDMMMYYYY dates)."""
    if not expiry_str:
        return dt.date.max
    text = str(expiry_str).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%Y%m%d", "%d%b%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return dt.date.max


class BSEOptionChainFetcher:

    BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api"

    SYMBOLS = {
        "SENSEX": {"scrip_cd": 1, "product_type": "IO", "strike_step": 100},
        "BANKEX": {"scrip_cd": 12, "product_type": "IO", "strike_step": 100},
        "SX50": {"scrip_cd": 47, "product_type": "IO", "strike_step": 100},
        "BSE FOCUSED IT": {"scrip_cd": 75, "product_type": "IO", "strike_step": 100},
    }

    def __init__(self, timeout: int = 15, max_retries: int = 3, backoff_base: float = 2.0):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Accept": "application/json",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
        })

    # ---------------------------------------------------------- utilities

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        text = str(value).strip().replace(",", "")
        if text in ("", "-", "--", "null", "None"):
            return default
        try:
            number = float(text)
            if math.isnan(number) or math.isinf(number):
                return default
            return number
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Retries transient failures with exponential backoff -- unlike
        nse_fetcher.py, this previously had zero retry logic, so a single
        timeout/5xx against BSE's similarly-flaky public endpoint killed the
        whole fetch immediately."""
        url = f"{self.BASE_URL}{endpoint}"
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError(f"Unexpected BSE response type: {type(data).__name__}")
                return data
            except Exception as e:
                last_exc = e
                log.warning(f"BSE request attempt {attempt}/{self.max_retries} failed for {endpoint}: {e}")
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base ** attempt)
        raise last_exc

    # -------------------------------------------------- expiry + strikes + spot

    def get_contract_metadata(self, symbol: str = "SENSEX") -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        if symbol not in self.SYMBOLS:
            raise ValueError(f"Unsupported BSE symbol: {symbol}. Supported: {', '.join(self.SYMBOLS)}")

        config = self.SYMBOLS[symbol]
        data = self._get("/ddlExpiry_IV/w", params={"ProductType": config["product_type"], "scrip_cd": config["scrip_cd"]})

        expiries = [row.get("ExpiryDate") for row in data.get("Table1", []) if row.get("ExpiryDate")]
        strikes = [self._number(row.get("StrikePrice")) for row in data.get("Table", []) if self._number(row.get("StrikePrice")) > 0]

        table2 = data.get("Table2", [])
        spot = self._number(table2[0].get("UlaValue")) if table2 else 0.0

        return {
            "symbol": symbol, "scrip_cd": config["scrip_cd"], "product_type": config["product_type"],
            "strike_step": config["strike_step"], "spot": spot, "expiries": expiries, "strikes": strikes,
        }

    # --------------------------------------------------------------- raw chain

    def get_raw_option_chain(self, symbol: str = "SENSEX", expiry: Optional[str] = None) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        metadata = self.get_contract_metadata(symbol)
        if not metadata["expiries"]:
            raise RuntimeError(f"No BSE expiry dates returned for {symbol}")
        if expiry is None:
            # Explicitly parse/filter/sort instead of blindly trusting
            # expiries[0] as "nearest" (nse_fetcher.get_nearest_expiry does
            # the same) -- if BSE's dropdown isn't strictly chronologically
            # ordered, or includes a stale/expired entry, index 0 can be wrong.
            today = _now_ist().date()
            parsed = [(e, _parse_bse_expiry(e)) for e in metadata["expiries"]]
            upcoming = [pair for pair in parsed if pair[1] >= today]
            pool = upcoming or parsed
            pool.sort(key=lambda pair: pair[1])
            expiry = pool[0][0]

        data = self._get("/DerivOptionChain_IV/w", params={"Expiry": expiry, "scrip_cd": metadata["scrip_cd"], "strprice": 0})
        return {"symbol": symbol, "expiry": expiry, "metadata": metadata, "data": data}

    # ------------------------------------------------------------ normalize CE/PE

    def _normalize_ce(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ltp": self._number(row.get("C_Last_Trd_Price")),
            "change": self._number(row.get("C_NetChange")),
            "oi": self._number(row.get("C_Open_Interest")),
            "change_oi": self._number(row.get("C_Absolute_Change_OI")),
            "volume": self._number(row.get("C_Vol_Traded")),
            "iv": self._number(row.get("C_IV")),
            "bid_qty": self._number(row.get("C_BIdQty")),
            "bid": self._number(row.get("C_BidPrice")),
            "ask": self._number(row.get("C_OfferPrice")),
            "ask_qty": self._number(row.get("C_OfferQty")),
        }

    def _normalize_pe(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ltp": self._number(row.get("Last_Trd_Price")),
            "change": self._number(row.get("NetChange")),
            "oi": self._number(row.get("Open_Interest")),
            "change_oi": self._number(row.get("Absolute_Change_OI")),
            "volume": self._number(row.get("Vol_Traded")),
            "iv": self._number(row.get("IV")),
            "bid_qty": self._number(row.get("BIdQty")),
            "bid": self._number(row.get("BidPrice")),
            "ask": self._number(row.get("OfferPrice")),
            "ask_qty": self._number(row.get("OfferQty")),
        }

    # --------------------------------------------------------- normalized chain

    def get_option_chain(self, symbol: str = "SENSEX", expiry: Optional[str] = None,
                          strikes_each_side: Optional[int] = None) -> Dict[str, Any]:
        raw_result = self.get_raw_option_chain(symbol=symbol, expiry=expiry)
        raw = raw_result["data"]
        metadata = raw_result["metadata"]
        rows = raw.get("Table", [])
        if not rows:
            raise RuntimeError(f"Empty BSE option chain for {symbol}")

        spot = 0.0
        for row in rows:
            spot = self._number(row.get("UlaValue"))
            if spot > 0:
                break
        if spot <= 0:
            spot = metadata["spot"]

        strike_step = metadata["strike_step"]
        atm = round(spot / strike_step) * strike_step if spot > 0 else 0

        chain = []
        for row in rows:
            strike = self._number(row.get("Strike_Price1") or row.get("Strike_Price"))
            if strike <= 0:
                continue
            chain.append({"strike": strike, "ce": self._normalize_ce(row), "pe": self._normalize_pe(row)})
        chain.sort(key=lambda item: item["strike"])

        if strikes_each_side is not None and strikes_each_side >= 0 and atm > 0:
            lo, hi = atm - strikes_each_side * strike_step, atm + strikes_each_side * strike_step
            chain = [item for item in chain if lo <= item["strike"] <= hi]

        totals = {
            "ce_oi": self._number(raw.get("tot_C_Open_Interest")),
            "pe_oi": self._number(raw.get("tot_Open_Interest")),
            "ce_volume": self._number(raw.get("tot_C_Vol_Traded")),
            "pe_volume": self._number(raw.get("tot_Vol_Traded")),
        }
        totals["pcr_oi"] = totals["pe_oi"] / totals["ce_oi"] if totals["ce_oi"] > 0 else 0.0
        totals["pcr_volume"] = totals["pe_volume"] / totals["ce_volume"] if totals["ce_volume"] > 0 else 0.0

        return {
            "exchange": "BSE", "symbol": raw_result["symbol"], "scrip_cd": metadata["scrip_cd"],
            "expiry": raw_result["expiry"], "spot": spot, "atm": atm, "strike_step": strike_step,
            "as_on": raw.get("ASON", {}).get("DT_TM", ""), "totals": totals, "chain": chain,
        }


# ------------------------------------------------------------------------
# Normalization into oi_engine.StrikeRow -- so the SAME PCR/Max Pain/S-R/
# bias logic that runs on Angel One / NSE data can run on BSE data too.
# ------------------------------------------------------------------------

def normalize_bse_chain(bse_result: dict, StrikeRowCls) -> dict:
    """Returns {strike: StrikeRow} keyed by int strike."""
    by_strike = {}
    for item in bse_result["chain"]:
        strike = int(item["strike"])
        ce, pe = item["ce"], item["pe"]
        row = StrikeRowCls(strike=strike)
        row.ce_oi, row.ce_oi_chg = int(ce["oi"]), int(ce["change_oi"])
        row.ce_vol, row.ce_ltp = int(ce["volume"]), ce["ltp"]
        row.ce_chg_pct = ce["change"]
        row.ce_iv = ce["iv"]
        row.ce_buy_qty, row.ce_sell_qty = int(ce["bid_qty"]), int(ce["ask_qty"])
        row.pe_oi, row.pe_oi_chg = int(pe["oi"]), int(pe["change_oi"])
        row.pe_vol, row.pe_ltp = int(pe["volume"]), pe["ltp"]
        row.pe_chg_pct = pe["change"]
        row.pe_iv = pe["iv"]
        row.pe_buy_qty, row.pe_sell_qty = int(pe["bid_qty"]), int(pe["ask_qty"])
        by_strike[strike] = row
    return by_strike


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetcher = BSEOptionChainFetcher()
    result = fetcher.get_option_chain(symbol="SENSEX", strikes_each_side=5)
    print(f"SENSEX spot={result['spot']} atm={result['atm']} expiry={result['expiry']}")
    print(f"Totals: {result['totals']}")
    for item in result["chain"]:
        print(item["strike"], "CE", item["ce"]["ltp"], item["ce"]["oi"], "| PE", item["pe"]["ltp"], item["pe"]["oi"])
