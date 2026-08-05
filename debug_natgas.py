import json

with open('instrument_master.json') as f:
    data = json.load(f)

print(f"Total instruments: {len(data)}")

matches = [r for r in data if 'NATURALGAS' in r.get('name', '').upper()]
print(f"\nRows with name containing NATURALGAS: {len(matches)}")

names = sorted(set(r.get('name') for r in matches))
print(f"Unique name values: {names}")

futcom = [r for r in data if r.get('name') == 'NATURALGAS' and r.get('instrumenttype') == 'FUTCOM' and r.get('exch_seg') == 'MCX']
print(f"\nFUTCOM candidates for name=='NATURALGAS', exch_seg=='MCX': {len(futcom)}")
futcom.sort(key=lambda r: r.get('expiry', ''))
for r in futcom[:5]:
    print(r.get('token'), r.get('symbol'), r.get('expiry'), r.get('lotsize'), r.get('tick_size'))

print("\n--- All exch_seg + instrumenttype combos found ---")
combos = sorted(set((r.get('exch_seg'), r.get('instrumenttype'), r.get('name')) for r in matches))
for c in combos[:20]:
    print(c)
