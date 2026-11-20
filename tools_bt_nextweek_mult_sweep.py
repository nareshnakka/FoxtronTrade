"""
Next-week dual: sweep exit multiples to find max-profit target per day / overall.
Walk-forward, same entry rules; only WIN_MULT changes. No look-ahead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Reuse next-week loader + merge/sim helpers by importing after patching WIN_MULT
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

OUT_JSON = Path("data/backtests/nextweek_mult_sweep.json")
OUT_TXT = Path("data/backtests/nextweek_mult_sweep.txt")

# Multiples to test (≈ profit target as × entry). 1.20 = +20%, 1.45 = +45%, …
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
    frames: list[dict],
    meta: dict,
    *,
    mult: float,
    lot_size: int,
    cfg: dict,
) -> dict[str, Any]:
    expiry = str(meta.get("expiry") or "")
    setups: list[dict] = []
    used_pairs: set[tuple[int, int]] = set()
    hunt_after = nw.MERGE_AFTER
    setup_id = 0
    day_net = 0.0
    old = nw.WIN_MULT
    nw.WIN_MULT = float(mult)
    try:
        while True:
            before = None if setup_id == 0 else nw.LAST_ENTRY_2ND
            if setup_id >= 1:
                if day_net >= nw.SKIP_2ND_IF_NET_GE:
                    break
                hunt_after = max(hunt_after, nw.SECOND_AFTER)
            merge = nw.find_merge_after(
                frames,
                after_t=hunt_after,
                before_t=before,
                exclude_pairs=used_pairs,
            )
            if not merge:
                break
            setup_id += 1
            lots = nw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
            res = nw.run_one_setup(
                frames,
                merge,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=setup_id,
                expiry=expiry,
            )
            setups.append(res)
            day_net = round(day_net + float(res["net"]), 2)
            used_pairs.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used_pairs.add((int(merge["pe_k"]), int(merge["ce_k"])))
            if setup_id >= nw.MAX_SETUPS:
                break
            exit_m = nw._hm_to_min(str(res.get("exit_t") or merge["t"])) + nw.COOLDOWN_MIN
            hunt_m = max(exit_m, nw._hm_to_min(nw.SECOND_AFTER))
            h, m = divmod(hunt_m, 60)
            hunt_after = f"{h:02d}:{m:02d}"
            if hunt_after >= nw.SESSION_END or hunt_after >= nw.LAST_ENTRY_2ND:
                break
    finally:
        nw.WIN_MULT = old

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
                "exit_why": s.get("exit_why"),
            }
            for s in setups
        ],
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = nw.available_days()

    # Cache frames once per day
    day_frames: dict[str, tuple[list[dict], dict]] = {}
    for day in days:
        day_frames[day] = nw.load_nextweek_frames(day)

    by_mult: dict[float, dict[str, Any]] = {}
    for mult in MULTS:
        daily = []
        for day in days:
            frames, meta = day_frames[day]
            if not frames:
                daily.append(
                    {
                        "day": day,
                        "mult": mult,
                        "net": 0,
                        "setup_count": 0,
                        "note": "no frames",
                    }
                )
                continue
            daily.append(
                simulate_day_at_mult(
                    day, frames, meta, mult=mult, lot_size=lot_size, cfg=cfg
                )
            )
        total_net = round(sum(float(d.get("net") or 0) for d in daily), 2)
        by_mult[mult] = {
            "mult": mult,
            "pct_gain": round((mult - 1.0) * 100, 1),
            "total_net": total_net,
            "daily": daily,
        }
        print(f"mult {mult:.2f}× (+{(mult-1)*100:.0f}%)  TOTAL NET ₹{total_net:,.2f}")

    # Per-day best mult
    day_best: list[dict[str, Any]] = []
    for day in days:
        rows = []
        for mult in MULTS:
            d = next(x for x in by_mult[mult]["daily"] if x["day"] == day)
            rows.append(d)
        best = max(rows, key=lambda x: float(x.get("net") or 0))
        worst = min(rows, key=lambda x: float(x.get("net") or 0))
        at_145 = next(x for x in rows if abs(float(x["mult"]) - 1.45) < 1e-9)
        day_best.append(
            {
                "day": day,
                "expiry": best.get("expiry"),
                "best_mult": best["mult"],
                "best_pct": best.get("pct_gain"),
                "best_net": best["net"],
                "best_setups": best.get("setup_count"),
                "net_at_1_45": at_145.get("net"),
                "worst_mult": worst["mult"],
                "worst_net": worst["net"],
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
        "strategy": "nextweek_dual_exit_mult_sweep",
        "mults": MULTS,
        "overall_best": {
            "mult": overall_best["mult"],
            "pct_gain": overall_best["pct_gain"],
            "total_net": overall_best["total_net"],
        },
        "at_1_45": {
            "mult": 1.45,
            "pct_gain": 45.0,
            "total_net": overall_145["total_net"],
        },
        "day_best": day_best,
        "by_mult": {
            f"{m:.2f}": {
                "total_net": by_mult[m]["total_net"],
                "pct_gain": by_mult[m]["pct_gain"],
            }
            for m in MULTS
        },
        "full": {f"{m:.2f}": by_mult[m] for m in MULTS},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("=" * 96)
    lines.append("NEXT-WEEK · exit-multiple sweep · max profit per day")
    lines.append(
        f"OVERALL BEST: {overall_best['mult']:.2f}× "
        f"(+{overall_best['pct_gain']:g}%)  NET ₹{overall_best['total_net']:,.2f}"
    )
    lines.append(
        f"vs 1.45× (+45%): NET ₹{overall_145['total_net']:,.2f}  "
        f"delta ₹{overall_best['total_net'] - overall_145['total_net']:+,.2f}"
    )
    lines.append("=" * 96)
    lines.append("")
    lines.append("TOTAL NET by exit mult:")
    for m in MULTS:
        mark = " ◀ BEST" if abs(m - overall_best["mult"]) < 1e-9 else ""
        mark145 = " (live)" if abs(m - 1.45) < 1e-9 else ""
        lines.append(
            f"  {m:.2f}×  (+{(m-1)*100:5.1f}%)  "
            f"NET ₹{by_mult[m]['total_net']:>12,.2f}{mark145}{mark}"
        )

    lines.append("")
    lines.append("-" * 96)
    lines.append("PER-DAY BEST TARGET")
    lines.append("-" * 96)
    for db in day_best:
        lines.append("")
        lines.append(
            f"### {db['day']}  exp={db.get('expiry')}  "
            f"BEST {db['best_mult']:.2f}× (+{db['best_pct']:g}%)  "
            f"NET ₹{db['best_net']:,.2f}  "
            f"(vs 1.45× ₹{db['net_at_1_45']:,.2f})"
        )
        for s in db.get("best_detail") or []:
            lines.append(
                f"  #{s['id']} {s['entry_t']}→{s['exit_t']}  win={s['win']}  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            lines.append(f"      CE {s['ce']}")
            lines.append(f"      PE {s['pe']}")
            lines.append(f"      {s.get('exit_why')}")
        lines.append("  curve:")
        for c in db["curve"]:
            star = " *" if abs(c["mult"] - db["best_mult"]) < 1e-9 else ""
            lines.append(
                f"    {c['mult']:.2f}× (+{c['pct']:5.1f}%)  "
                f"setups={c['setups']}  NET ₹{c['net']:>10,.2f}{star}"
            )

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print("=" * 72)
    print(
        f"OVERALL BEST {overall_best['mult']:.2f}× (+{overall_best['pct_gain']:g}%)  "
        f"NET ₹{overall_best['total_net']:,.2f}"
    )
    print(f"1.45× live baseline NET ₹{overall_145['total_net']:,.2f}")
    print()
    for db in day_best:
        print(
            f"{db['day']}  BEST {db['best_mult']:.2f}× (+{db['best_pct']:g}%)  "
            f"₹{db['best_net']:,.2f}  |  1.45× ₹{db['net_at_1_45']:,.2f}"
        )
    print("-" * 72)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
