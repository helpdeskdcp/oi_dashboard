import requests
import json
import time

BASE_URL = "https://www.nseindia.com"
API_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}


def fetch_nifty_option_chain():
    session = requests.Session()
    session.headers.update(HEADERS)

    print("🔄 Creating NSE session...")

    # First request: obtain cookies
    home = session.get(
        BASE_URL,
        timeout=15
    )

    print(f"🌐 NSE Home Status: {home.status_code}")
    print(f"🍪 Cookies: {session.cookies.get_dict()}")

    time.sleep(1)

    print("\n🔄 Fetching NIFTY option chain...")

    response = session.get(
        API_URL,
        timeout=15
    )

    print(f"📡 API Status: {response.status_code}")
    print(f"📦 Content-Type: {response.headers.get('content-type')}")

    if response.status_code != 200:
        print("\n❌ Request failed")
        print(response.text[:1000])
        return

    try:
        data = response.json()
    except json.JSONDecodeError:
        print("\n❌ Response is not valid JSON")
        print(response.text[:1000])
        return

    records = data.get("records", {})

    spot = records.get("underlyingValue")
    timestamp = records.get("timestamp")
    expiries = records.get("expiryDates", [])
    option_data = records.get("data", [])

    print("\n" + "=" * 70)
    print("               NIFTY OPTION CHAIN")
    print("=" * 70)

    print(f"📊 NIFTY Spot : {spot}")
    print(f"🕒 Timestamp  : {timestamp}")
    print(f"📅 Expiries   : {expiries}")
    print(f"📈 Records    : {len(option_data)}")

    if not option_data:
        print("\n⚠️ No option-chain records found.")
        return

    # Nearest expiry
    nearest_expiry = expiries[0] if expiries else None

    print(f"\n🎯 Selected Expiry: {nearest_expiry}")

    filtered = [
        row for row in option_data
        if row.get("expiryDate") == nearest_expiry
    ]

    # Sort by strike
    filtered.sort(key=lambda x: x.get("strikePrice", 0))

    print("\n")
    print(
        f"{'CE OI':>12} "
        f"{'CE CHG OI':>12} "
        f"{'CE LTP':>10} "
        f"{'STRIKE':>10} "
        f"{'PE LTP':>10} "
        f"{'PE CHG OI':>12} "
        f"{'PE OI':>12}"
    )

    print("-" * 85)

    for row in filtered:
        strike = row.get("strikePrice", 0)

        ce = row.get("CE", {})
        pe = row.get("PE", {})

        ce_oi = ce.get("openInterest", 0)
        ce_change_oi = ce.get("changeinOpenInterest", 0)
        ce_ltp = ce.get("lastPrice", 0)

        pe_ltp = pe.get("lastPrice", 0)
        pe_change_oi = pe.get("changeinOpenInterest", 0)
        pe_oi = pe.get("openInterest", 0)

        print(
            f"{ce_oi:>12} "
            f"{ce_change_oi:>12} "
            f"{ce_ltp:>10.2f} "
            f"{strike:>10.2f} "
            f"{pe_ltp:>10.2f} "
            f"{pe_change_oi:>12} "
            f"{pe_oi:>12}"
        )

    # Save complete JSON
    with open("nifty_option_chain.json", "w") as f:
        json.dump(data, f, indent=2)

    print("\n✅ Full JSON saved: nifty_option_chain.json")


if __name__ == "__main__":
    fetch_nifty_option_chain()
