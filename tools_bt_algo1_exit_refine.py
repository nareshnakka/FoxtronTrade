"""Focused causal refinements aiming for every-day green on last 5."""
from __future__ import annotations

import json

import tools_bt_algo1_exit_learn as L
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    all_days = cw.available_days()
    frames: dict = {}
    for d in all_days:
        v = json.loads((cw.CHAIN / d / "viewer.json").read_text(encoding="utf-8"))
        frames[d] = list(v.get("frames") or [])
    last5 = [d for d in L.LAST5 if d in frames]

    extra = [
        L.Policy("trail13_dd10_cut11L", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="11:00", cut_if_losing=True),
        L.Policy("trail13_dd10_cut11A", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="11:00", cut_if_losing=False),
        L.Policy("trail13_dd12_cut11L", 1.50, "trail", retrace_pct=12, arm_mult=1.30, cut_by="11:00", cut_if_losing=True),
        L.Policy("trail125_dd10_cut11L", 1.50, "trail", retrace_pct=10, arm_mult=1.25, cut_by="11:00", cut_if_losing=True),
        L.Policy("trail13_dd10_max1", 1.50, "trail", retrace_pct=10, arm_mult=1.30, max_setups=1),
        L.Policy("trail13_dd10_skip2k5", 1.50, "trail", retrace_pct=10, arm_mult=1.30, skip_2nd_if_net_ge=5000),
        L.Policy("trail13_dd10_skip2k3", 1.50, "trail", retrace_pct=10, arm_mult=1.30, skip_2nd_if_net_ge=3000),
        L.Policy("trail13_dd10_nolossre", 1.50, "trail", retrace_pct=10, arm_mult=1.30, loss_reentry_after="15:00"),
        L.Policy("hit15_cut1030L", 1.50, "hit", cut_by="10:30", cut_if_losing=True),
        L.Policy("hit15_cut11L_max1", 1.50, "hit", cut_by="11:00", cut_if_losing=True, max_setups=1),
        L.Policy("trail13_dd8_cut11L_max1", 1.50, "trail", retrace_pct=8, arm_mult=1.30, cut_by="11:00", cut_if_losing=True, max_setups=1),
        L.Policy("trail14_dd10", 1.50, "trail", retrace_pct=10, arm_mult=1.40),
        L.Policy("trail13_dd10_cut12L", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="12:00", cut_if_losing=True),
        L.Policy("trail13_dd10_lockgreen", 1.50, "trail", retrace_pct=10, arm_mult=1.30, skip_2nd_if_net_ge=1),
        L.Policy("hit145_lockgreen", 1.45, "hit", skip_2nd_if_net_ge=1),
        L.Policy("hit150_lockgreen", 1.50, "hit", skip_2nd_if_net_ge=1),
        L.Policy("trail13_dd10_cut11L_lock", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="11:00", cut_if_losing=True, skip_2nd_if_net_ge=1),
        L.Policy("baseline", 1.45, "hit"),
        L.Policy("trail13_dd10", 1.50, "trail", retrace_pct=10, arm_mult=1.30),
        # lock green after ANY positive first setup; cut morning losers @11
        L.Policy("best_combo", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="11:00", cut_if_losing=True, skip_2nd_if_net_ge=1, loss_reentry_after="15:00"),
        L.Policy("best_combo_max1", 1.50, "trail", retrace_pct=10, arm_mult=1.30, cut_by="11:00", cut_if_losing=True, max_setups=1),
    ]

    rows = []
    for p in extra:
        d5, s5 = L.run_universe(last5, frames, policy=p, lot_size=lot, cfg=cfg)
        da, sa = L.run_universe(all_days, frames, policy=p, lot_size=lot, cfg=cfg)
        rows.append(
            {
                "name": p.name,
                "last5": s5,
                "all": sa,
                "daily": [{"day": x["day"], "net": x["net"], "n": x["setup_count"]} for x in d5],
                "policy": p,
            }
        )

    rows.sort(
        key=lambda r: (r["last5"]["green_days"], r["last5"]["worst_day"], r["last5"]["net"]),
        reverse=True,
    )

    print("Focused sweep — last5 ranked by green / worst / net")
    print("=" * 96)
    for r in rows:
        s5, sa = r["last5"], r["all"]
        flag = " ★5/5" if s5["green_days"] == 5 and s5["red_days"] == 0 else ""
        print(
            f"{r['name']:<34} L5 {s5['green_days']}G/{s5['red_days']}R "
            f"net={s5['net']:>8,.0f} worst={s5['worst_day']:>8,.0f}  "
            f"ALL {sa['green_days']}G net={sa['net']:>8,.0f}{flag}"
        )
        bits = [f"{x['day'][5:]} {x['net']:,.0f}({x['n']})" for x in r["daily"]]
        print("  " + " | ".join(bits))

    best = rows[0]
    print("\nBEST:", best["name"], best["last5"])
    # show trades
    d5, _ = L.run_universe(last5, frames, policy=best["policy"], lot_size=lot, cfg=cfg)
    for day in d5:
        print(f"\n{day['day']} NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            print(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']} "
                f"CE {s['ce_mult']}× PE {s['pe_mult']}× NET ₹{s['net']:,.2f} | {s['exit_why']}"
            )


if __name__ == "__main__":
    main()
