"""
Current-week dual: sweep exit multiples for max-profit target.
Walk-forward; same entry rules as tools_bt_final_daily_report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

OUT_JSON = Path("data/backtests/currentweek_mult_sweep.json")
OUT_TXT = Path("data/backtests/currentweek_mult_sweep.txt")

MULTS = [
    1.20,
    1.25,
    1.30,
    1.35,
    1.40,
    1.45,
    1.50,
    1.55,
    1.60,
    1.70,
    1.80,
    1.90,
    2.00,
    2.25,
    2.50,
]


def simulate_day_at_mult(
    day: str,
    *,
    mult: float,
    lot_size: int,
    cfg: dict,
) -> dict[str, Any]:
    viewer_path = cw.CHAIN / day / "viewer.json"
    viewer = json.loads(viewer_path.read_text(encoding="utf-8"))
    frames = list(viewer.get("frames") or [])
    expiry = str((viewer.get("meta") or {}).get("expiry") or "")
    if not frames:
        return {
            "day": day,
            "expiry": expiry,
            "mult": mult,
            "pct_gain": round((mult - 1.0) * 100, 1),
            "net": 0,
            "setup_count": 0,
            "setups": [],
        }

    setups: list[dict] = []
    used_pairs: set[tuple[int, int]] = set()
    hunt_after = cw.MERGE_AFTER
    setup_id = 0
    day_net = 0.0
    old = cw.WIN_MULT
    cw.WIN_MULT = float(mult)
    try:
        while True:
            before = None if setup_id == 0 else cw.LAST_ENTRY_2ND
            if setup_id >= 1:
                if day_net >= cw.SKIP_2ND_IF_NET_GE:
                    break
                hunt_after = max(hunt_after, cw.SECOND_AFTER)
            merge = cw.find_merge_after(
                frames,
                after_t=hunt_after,
                before_t=before,
                exclude_pairs=used_pairs,
            )
            if not merge:
                break
            setup_id += 1
            lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
            res = cw.run_one_setup(
                frames,
                merge,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=setup_id,
            )
            setups.append(res)
            day_net = round(day_net + float(res["net"]), 2)
            used_pairs.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used_pairs.add((int(merge["pe_k"]), int(merge["ce_k"])))
            if setup_id >= cw.MAX_SETUPS:
                break
            exit_m = cw._hm_to_min(str(res.get("exit_t") or merge["t"])) + cw.COOLDOWN_MIN
            hunt_m = max(exit_m, cw._hm_to_min(cw.SECOND_AFTER))
            h, m = divmod(hunt_m, 60)
            hunt_after = f"{h:02d}:{m:02d}"
            if hunt_after >= cw.SESSION_END or hunt_after >= cw.LAST_ENTRY_2ND:
                break
    finally:
        cw.WIN_MULT = old

    return {
        "day": day,
        "expiry": expiry,
        "mult": mult,
        "pct_gain": round((mult - 1.0) * 100, 1),
        "setup_count": len(setups),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "setups": [
            {
                "id": s["setup_id"],
                "entry_t": s["entry_t"],
                "exit_t": s["exit_t"],
                "win": s["win_side"],
                "ce": f"{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)",
                "pe": f"{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)",
                "lots": s["lots"],
                "net": s["net"],
            }
            for s in setups
        ],
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = cw.available_days()

    by_mult: dict[float, dict[str, Any]] = {}
    for mult in MULTS:
        daily = [simulate_day_at_mult(d, mult=mult, lot_size=lot_size, cfg=cfg) for d in days]
        total_net = round(sum(float(d.get("net") or 0) for d in daily), 2)
        by_mult[mult] = {
            "mult": mult,
            "pct_gain": round((mult - 1.0) * 100, 1),
            "total_net": total_net,
            "daily": daily,
        }
        print(f"mult {mult:.2f}× (+{(mult-1)*100:.0f}%)  TOTAL NET ₹{total_net:,.2f}")

    day_best: list[dict[str, Any]] = []
    for day in days:
        rows = [next(x for x in by_mult[m]["daily"] if x["day"] == day) for m in MULTS]
        best = max(rows, key=lambda x: float(x.get("net") or 0))
        at_145 = next(x for x in rows if abs(float(x["mult"]) - 1.45) < 1e-9)
        day_best.append(
            {
                "day": day,
                "expiry": best.get("expiry"),
                "best_mult": best["mult"],
                "best_pct": best.get("pct_gain"),
                "best_net": best["net"],
                "net_at_1_45": at_145.get("net"),
                "curve": [
                    {
                        "mult": r["mult"],
                        "pct": r.get("pct_gain"),
                        "net": r.get("net"),
                        "setups": r.get("setup_count"),
                    }
                    for r in rows
                ],
                "best_detail": best.get("setups"),
            }
        )

    overall_best = max(by_mult.values(), key=lambda x: float(x["total_net"]))
    overall_145 = by_mult[1.45]

    report = {
        "strategy": "currentweek_dual_exit_mult_sweep",
        "overall_best": {
            "mult": overall_best["mult"],
            "pct_gain": overall_best["pct_gain"],
            "total_net": overall_best["total_net"],
        },
        "at_1_45": {"mult": 1.45, "pct_gain": 45.0, "total_net": overall_145["total_net"]},
        "day_best": day_best,
        "by_mult": {
            f"{m:.2f}": {"total_net": by_mult[m]["total_net"], "pct_gain": by_mult[m]["pct_gain"]}
            for m in MULTS
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 96,
        "CURRENT-WEEK · exit-multiple sweep · max profit",
        f"OVERALL BEST: {overall_best['mult']:.2f}× (+{overall_best['pct_gain']:g}%)  "
        f"NET ₹{overall_best['total_net']:,.2f}",
        f"vs 1.45× (+45%): NET ₹{overall_145['total_net']:,.2f}  "
        f"delta ₹{overall_best['total_net'] - overall_145['total_net']:+,.2f}",
        "=" * 96,
        "",
        "TOTAL NET by exit mult:",
    ]
    for m in MULTS:
        mark = " ◀ BEST" if abs(m - overall_best["mult"]) < 1e-9 else ""
        mark145 = " (live)" if abs(m - 1.45) < 1e-9 else ""
        lines.append(
            f"  {m:.2f}×  (+{(m-1)*100:5.1f}%)  "
            f"NET ₹{by_mult[m]['total_net']:>12,.2f}{mark145}{mark}"
        )
    lines.append("")
    lines.append("PER-DAY BEST TARGET")
    for db in day_best:
        lines.append(
            f"  {db['day']}  BEST {db['best_mult']:.2f}× (+{db['best_pct']:g}%)  "
            f"NET ₹{db['best_net']:,.2f}  |  1.45× ₹{db['net_at_1_45']:,.2f}"
        )

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print("=" * 72)
    print(
        f"OVERALL BEST {overall_best['mult']:.2f}× (+{overall_best['pct_gain']:g}%)  "
        f"NET ₹{overall_best['total_net']:,.2f}"
    )
    print(f"1.45× live baseline NET ₹{overall_145['total_net']:,.2f}")
    for db in day_best:
        print(
            f"{db['day']}  BEST {db['best_mult']:.2f}× (+{db['best_pct']:g}%)  "
            f"₹{db['best_net']:,.2f}  |  1.45× ₹{db['net_at_1_45']:,.2f}"
        )
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
