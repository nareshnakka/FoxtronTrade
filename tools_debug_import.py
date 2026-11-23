from pathlib import Path
import inspect
import foxtrade.engine as e

sp = Path(".venv/Lib/site-packages")
print("site hits", list(sp.glob("*foxtrade*")) + list(sp.glob("*Nifty*")))
print("file", e.__file__)
text = Path(e.__file__).read_text(encoding="utf-8")
print("algo_names in file", "algo_names" in text)
src = inspect.getsource(e.TradingEngine.dashboard_state)
print("algo_names in loaded source", "algo_names" in src)
print("algo_cw key in loaded source", '"algo_cw"' in src)
print("headline Algo-CW in loaded", "Algo-CW" in src)
st = e.ENGINE.dashboard_state()
print("live keys", [k for k in st if str(k).startswith("algo")])
print("headline", (st.get("activity") or {}).get("headline"))
