import json
from pathlib import Path

d = json.loads(Path("data/backtests/overnight_final_multi.json").read_text(encoding="utf-8"))
print("composite", d.get("composite_score"))
print("capital", d.get("capital"), "deploy", d.get("deploy_pct"))
print("---")
for b in d["periods"]:
    s = b["summary"]
    la = b.get("loss_analysis") or {}
    print(
        f"{b['days']:3d}d  G/R {s.get('green_days')}/{s.get('red_days')}  "
        f"traded={s.get('traded_days')}  "
        f"net={float(s.get('net_pnl') or 0):+.0f}  "
        f"ret={float(s.get('return_pct') or 0):+.1f}%  "
        f"ge5={la.get('pct_traded_days_ge_5')}%  "
        f"early_stops={la.get('early_stop_losses')}  "
        f"med_day%={la.get('median_traded_day_pct')}"
    )
