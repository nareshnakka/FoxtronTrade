import json
from pathlib import Path

old = json.loads(Path("data/backtests/multi_1l_d75_report.json").read_text(encoding="utf-8"))
new = json.loads(Path("data/backtests/overnight_final_multi.json").read_text(encoding="utf-8"))
print(f"{'days':>4}  {'OLD G/R':>10} {'OLD ret':>9}  {'NEW G/R':>10} {'NEW ret':>9}  {'Δret':>8}")
for ob, nb in zip(old["periods"], new["periods"]):
    os_, ns_ = ob["summary"], nb["summary"]
    dret = float(ns_.get("return_pct") or 0) - float(os_.get("return_pct") or 0)
    print(
        f"{ob['days']:4d}  "
        f"{os_.get('green_days')}/{os_.get('red_days'):<8} "
        f"{float(os_.get('return_pct') or 0):+8.1f}%  "
        f"{ns_.get('green_days')}/{ns_.get('red_days'):<8} "
        f"{float(ns_.get('return_pct') or 0):+8.1f}%  "
        f"{dret:+7.1f}%"
    )
