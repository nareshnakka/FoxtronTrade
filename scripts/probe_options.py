from datetime import datetime
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite

kite = get_authenticated_kite()
inst = kite.instruments("NFO")
nifty = [i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
exps = sorted({i["expiry"] for i in nifty})
today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
future = [e for e in exps if e >= today]
print("next expiries", future[:4])
spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
print("spot", spot)
atm = round(spot / 50) * 50
for exp in future[:2]:
    subset = [i for i in nifty if i["expiry"] == exp and abs(i["strike"] - atm) <= 500]
    keys = [f"NFO:{i['tradingsymbol']}" for i in subset]
    quotes = {}
    for i in range(0, len(keys), 400):
        quotes.update(kite.ltp(keys[i : i + 400]))
    inband = []
    for i in subset:
        lp = quotes.get(f"NFO:{i['tradingsymbol']}", {}).get("last_price")
        if lp and 125 <= lp <= 145:
            inband.append((i["tradingsymbol"], i["instrument_type"], i["strike"], lp))
    print(exp, "inband", len(inband), inband[:12])
