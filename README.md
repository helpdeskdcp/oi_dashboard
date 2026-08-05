# {{SYMBOL}} OI Scalping Dashboard — Browser Version (Flask + SocketIO)

Chanakya AI style live browser dashboard. Angel One = primary data, NSE = best-effort.
No Excel — sab kuch browser me live, WebSocket push updates, chart, alerts.

## Setup (VPS ya Termux)

```bash
cd flask_dashboard
pip install -r requirements.txt --break-system-packages
cp .env.example .env
nano .env    # Angel One credentials daalo
python3 app.py
```

Browser me kholo:
- Termux/phone pe khud: `http://127.0.0.1:5050`
- VPS pe: `http://<vps-ip>:5050` (firewall me port 5050 open karna padega, ya nginx reverse-proxy laga do jaise Chanakya v5 ka hai)

Background me chalane ke liye:
```bash
nohup python3 app.py > flask_dashboard.log 2>&1 &
```

## Kya milega

- **Live cards**: LTP, ATM, PCR, Max Pain, Support/Resistance, NSE feed status
- **Bias banner**: color-coded (green=bullish, red=bearish, yellow=neutral) with reasoning note
- **Live chart**: LTP + PCR over last ~200 cycles
- **Option chain table**: ATM ±4 CE/PE — OI, OI change, Volume, LTP, %chg, buildup signal (color-coded)
- **Alerts panel**: jab bhi bias change hota hai (e.g. NEUTRAL → BULLISH BREAKOUT), alert list me aata hai + browser notification (permission allow karna) + optional Telegram push agar `.env` me `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` bhara ho

## Same caveats jo Excel version me the

1. NSE Akamai bot-detection ke wajah se kabhi bhi block ho sakta hai — dashboard "NSE Feed" card me status dikhayega, poora app nahi rukega (Angel One primary hai).
2. Instrument-master token matching verify kar lena ek baar — agar option chain table me OI/LTP zero dikhe, `find_option_token` ka strike-format check karna (`instrument_master.json` khol ke).
3. Weekend/market-closed hours me LTP/OI static rahega — ye normal hai, bug nahi.
4. Ye sandbox environment me test nahi ho saka (NSE/Angel One domains tak network access nahi) — apne VPS/Termux pe pehla run dhyan se dekhna.

## Disclaimer

Analytical/informational tool hai, guaranteed trading signal nahi. Apna risk management khud follow karo.
