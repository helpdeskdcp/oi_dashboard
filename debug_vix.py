import json

with open('instrument_master.json') as f:
    data = json.load(f)

matches = [r for r in data if 'VIX' in r.get('symbol', '').upper() or 'VIX' in r.get('name', '').upper()]
print(f"VIX-related rows found: {len(matches)}\n")
for r in matches[:15]:
    print(f"symbol={r.get('symbol')!r}  name={r.get('name')!r}  token={r.get('token')}  "
          f"exch_seg={r.get('exch_seg')}  instrumenttype={r.get('instrumenttype')!r}")
