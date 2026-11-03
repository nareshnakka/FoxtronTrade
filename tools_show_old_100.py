import json
from pathlib import Path

d = json.loads(Path("data/backtests/multi_1l_d75_report.json").read_text(encoding="utf-8"))
print("Report:", d["title"])
print("Generated:", d["generated_at"])
print("Capital: ₹{:,.0f}  deploy: {:.0%}".format(d["capital"], d["deploy_pct"]))
print()
print(f"{'days':>4}  {'range':^23}  {'sess':>4}  {'trades':>6}  {'W/L':>7}  {'G/R':>7}  {'net':>12}  {'ret':>8}")
for b in d["periods"]:
    s = b["summary"]
    print(
        f"{b['days']:4d}  "
        f"{s.get('range_start')}→{s.get('range_end')}  "
        f"{s.get('session_days'):4d}  "
        f"{s.get('setups'):6d}  "
        f"{s.get('wins')}/{s.get('losses'):<5}  "
        f"{s.get('green_days')}/{s.get('red_days'):<5}  "
        f"₹{float(s.get('net_pnl') or 0):>+10,.0f}  "
        f"{float(s.get('return_pct') or 0):>+7.1f}%"
    )

# highlight ~100% windows
print("\n~100% windows:")
for b in d["periods"]:
    r = float(b["summary"].get("return_pct") or 0)
    if r >= 95:
        s = b["summary"]
        print(
            f"  {b['days']}d → {r:+.1f}%  "
            f"(₹{s['net_pnl']:+,.0f} · {s['setups']} trades · "
            f"G/R {s['green_days']}/{s['red_days']} · "
            f"post-decay entries {s.get('post_decay_entries')})"
        )
